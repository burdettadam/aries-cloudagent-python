"""Classes to manage connections."""

import logging

from typing import Coroutine, Sequence, Tuple

from ....cache.base import BaseCache
from ....connections.models.conn_record import ConnRecord
from ....connections.models.connection_target import ConnectionTarget
from ....connections.models.diddoc import (
    DIDDoc,
    PublicKey,
    PublicKeyType,
    Service,
)
from ....config.base import InjectionError
from ....core.error import BaseError
from ....core.profile import ProfileSession
from ....ledger.base import BaseLedger
from ....messaging.responder import BaseResponder
from ....messaging.models.base_record import BaseRecord
from ....storage.base import BaseStorage
from ....storage.error import StorageError, StorageNotFoundError
from ....storage.record import StorageRecord
from ....transport.inbound.receipt import MessageReceipt
from ....wallet.base import BaseWallet, DIDInfo
from ....wallet.crypto import create_keypair, seed_to_did
from ....wallet.error import WalletNotFoundError
from ....wallet.util import bytes_to_b58, did_key_to_naked
from ....protocols.routing.v1_0.manager import RoutingManager

from .messages.connection_invitation import ConnectionInvitation
from .messages.connection_request import ConnectionRequest
from .messages.connection_response import ConnectionResponse
from .messages.problem_report import ProblemReportReason
from .models.connection_detail import ConnectionDetail
from ...coordinate_mediation.v1_0.messages.inner.keylist_update_rule import (
    KeylistUpdateRule
)
from ...coordinate_mediation.v1_0.messages.keylist_update import KeylistUpdate
from ...coordinate_mediation.v1_0.models.mediation_record import MediationRecord


class ConnectionManagerError(BaseError):
    """Connection error."""


class ConnectionManager:
    """Class for managing connections."""

    RECORD_TYPE_DID_DOC = "did_doc"
    RECORD_TYPE_DID_KEY = "did_key"

    def __init__(self, session: ProfileSession):
        """
        Initialize a ConnectionManager.

        Args:
            session: The profile session for this connection manager
        """
        self._session = session
        self._logger = logging.getLogger(__name__)

    @property
    def session(self) -> ProfileSession:
        """
        Accessor for the current profile session.

        Returns:
            The profile session for this connection manager

        """
        return self._session

    async def create_invitation(
        self,
        my_label: str = None,
        my_endpoint: str = None,
        auto_accept: bool = None,
        public: bool = False,
        multi_use: bool = False,
        alias: str = None,
        routing_keys: Sequence[str] = None,
        recipient_keys: Sequence[str] = None,
        mediation_id: str = None,
    ) -> Tuple[ConnRecord, ConnectionInvitation]:
        """
        Generate new connection invitation.

        This interaction represents an out-of-band communication channel. In the future
        and in practice, these sort of invitations will be received over any number of
        channels such as SMS, Email, QR Code, NFC, etc.

        Structure of an invite message:

        ::

            {
                "@type": "https://didcomm.org/connections/1.0/invitation",
                "label": "Alice",
                "did": "did:sov:QmWbsNYhMrjHiqZDTUTEJs"
            }

        Or, in the case of a peer DID:

        ::

            {
                "@type": "https://didcomm.org/connections/1.0/invitation",
                "label": "Alice",
                "did": "did:peer:oiSqsNYhMrjHiqZDTUthsw",
                "recipient_keys": ["8HH5gYEeNc3z7PYXmd54d4x6qAfCNrqQqEB3nS7Zfu7K"],
                "service_endpoint": "https://example.com/endpoint"
                "routing_keys": ["9EH5gYEeNc3z7PYXmd53d5x6qAfCNrqQqEB4nS7Zfu6K"],
            }

        Args:
            my_label: label for this connection
            my_endpoint: endpoint where other party can reach me
            auto_accept: auto-accept a corresponding connection request
                (None to use config)
            public: set to create an invitation from the public DID
            multi_use: set to True to create an invitation for multiple use
            alias: optional alias to apply to connection for later use

        Returns:
            A tuple of the new `ConnRecord` and `ConnectionInvitation` instances

        """
        if not my_label:
            my_label = self._session.settings.get("default_label")
        wallet = self._session.inject(BaseWallet)
        if public:
            if not self._session.settings.get("public_invites"):
                raise ConnectionManagerError("Public invitations are not enabled")

            public_did = await wallet.get_public_did()
            if not public_did:
                raise ConnectionManagerError(
                    "Cannot create public invitation with no public DID"
                )

            if multi_use:
                raise ConnectionManagerError(
                    "Cannot use public and multi_use at the same time"
                )

            # FIXME - allow ledger instance to format public DID with prefix?
            invitation = ConnectionInvitation(
                label=my_label, did=f"did:sov:{public_did.did}"
            )
            return None, invitation
        mediation_record = None
        invitation_mode = ConnRecord.INVITATION_MODE_ONCE
        if multi_use:
            invitation_mode = ConnRecord.INVITATION_MODE_MULTI
        if mediation_id:
            mediation_record = await MediationRecord.retrieve_by_id(
                self._session,
                mediation_id
            )
        if not my_endpoint:
            if mediation_record:
                my_endpoint = mediation_record.endpoint
            else:
                my_endpoint = self._session.settings.get("default_endpoint")
        accept = (
            ConnRecord.ACCEPT_AUTO
            if (
                auto_accept
                or (
                    auto_accept is None
                    and self._session.settings.get("debug.auto_accept_requests")
                )
            )
            else ConnRecord.ACCEPT_MANUAL
        )

        if recipient_keys:
            # TODO: check that recipient keys are in wallet
            invitation_key = recipient_keys[0]  # TODO: use all keys?
        else:
            # Create and store new invitation key
            invitation_signing_key = await wallet.create_signing_key()
            invitation_key = invitation_signing_key.verkey
            recipient_keys = [invitation_key]
            if mediation_record and self._session.settings.get(
                "mediation.auto_send_keylist_update_in_create_invitation"
            ):
                # send a update keylist message with new recipient keys.
                updates = [
                    KeylistUpdateRule(
                        recipient_key=invitation_key,
                        action=KeylistUpdateRule.RULE_ADD
                    )
                ]
                responder = self._session.inject(BaseResponder, required=False)
                update_keylist_request = KeylistUpdate(updates=updates)
                await responder.send_reply(
                    update_keylist_request,
                    connection_id=mediation_record.connection_id
                )
        if not routing_keys and mediation_record:
            routing_keys = mediation_record.routing_keys
        # Create connection record
        connection = ConnRecord(
            invitation_key=invitation_key,  # TODO: determine correct key to use
            their_role=ConnRecord.Role.REQUESTER.rfc160,
            state=ConnRecord.State.INVITATION.rfc160,
            accept=accept,
            invitation_mode=invitation_mode,
            alias=alias,
        )

        await connection.save(self._session, reason="Created new invitation")

        # Create connection invitation message
        # Note: Need to split this into two stages to support inbound routing of invites
        # Would want to reuse create_did_document and convert the result
        invitation = ConnectionInvitation(
            label=my_label,
            recipient_keys=recipient_keys,
            endpoint=my_endpoint,
            routing_keys=routing_keys,
        )
        await connection.attach_invitation(self._session, invitation)

        return connection, invitation

    async def receive_invitation(
        self,
        invitation: ConnectionInvitation,
        auto_accept: bool = None,
        alias: str = None,
        routing_options: BaseRecord = None
    ) -> ConnRecord:
        """
        Create a new connection record to track a received invitation.

        Args:
            invitation: The `ConnectionInvitation` to store
            auto_accept: set to auto-accept the invitation (None to use config)
            alias: optional alias to set on the record

        Returns:
            The new `ConnRecord` instance

        """
        if not invitation.did:
            if not invitation.recipient_keys:
                raise ConnectionManagerError("Invitation must contain recipient key(s)")
            if not invitation.endpoint:
                raise ConnectionManagerError("Invitation must contain an endpoint")
        if routing_options:
            if not hasattr(routing_options, "routing_keys"):
                raise ConnectionManagerError("routing_options must contain routing keys")
        accept = (
            ConnRecord.ACCEPT_AUTO
            if (
                auto_accept
                or (
                    auto_accept is None
                    and self._session.settings.get("debug.auto_accept_invites")
                )
            )
            else ConnRecord.ACCEPT_MANUAL
        )

        # Create connection record
        connection = ConnRecord(
            invitation_key=invitation.recipient_keys[0],
            their_label=invitation.label,
            their_role=ConnRecord.Role.RESPONDER.rfc160,
            state=ConnRecord.State.INVITATION.rfc160,
            accept=accept,
            alias=alias,
        )

        await connection.save(
            self._session,
            reason="Created new connection record from invitation",
            log_params={"invitation": invitation, "their_label": invitation.label},
        )

        # Save the invitation for later processing
        await connection.attach_invitation(self._session, invitation)

        if connection.accept == ConnRecord.ACCEPT_AUTO:
            request = await self.create_request(
                connection,
                routing_options=routing_options
                )
            responder = self._session.inject(BaseResponder, required=False)
            if responder:
                await responder.send(request, connection_id=connection.connection_id)
                # refetch connection for accurate state
                connection = await ConnRecord.retrieve_by_id(
                    self._session, connection.connection_id
                )
        else:
            self._logger.debug("Connection invitation will await acceptance")
        return connection

    async def create_request(
        self,
        connection: ConnRecord,
        my_label: str = None,
        my_endpoint: str = None,
        routing_options: BaseRecord = None
    ) -> ConnectionRequest:
        """
        Create a new connection request for a previously-received invitation.

        Args:
            connection: The `ConnRecord` representing the invitation to accept
            my_label: My label
            my_endpoint: My endpoint

        Returns:
            A new `ConnectionRequest` message to send to the other agent

        """
        my_info = None
        wallet = self._session.inject(BaseWallet)
        if connection.my_did:
            my_info = await wallet.get_local_did(connection.my_did)
        else:
            # Create new DID for connection
            my_info = await wallet.create_local_did()
            connection.my_did = my_info.did
            if hasattr(connection, "routing_keys") and self._session.settings.get(
                "mediation.auto_send_keylist_update_in_requests"
            ):
                if routing_options and hasattr(routing_options, "connection_id"):
                    target_connection_id = routing_options.connection_id
                else:
                    target_connection_id = connection.recipient_keys[0]
                # send a update keylist message with new recipient keys.
                updates = [
                    KeylistUpdateRule(
                        recipient_key=my_info.verkey,
                        action=KeylistUpdateRule.RULE_ADD
                    )
                ]
                responder = self._session.inject(BaseResponder, required=False)
                update_keylist_request = KeylistUpdate(updates=updates)
                await responder.send_reply(
                    update_keylist_request,
                    connection_id=target_connection_id
                )
        # Create connection request message
        if my_endpoint:
            my_endpoints = [my_endpoint]
        else:
            my_endpoints = []
            default_endpoint = self._session.settings.get("default_endpoint")
            if default_endpoint:
                my_endpoints.append(default_endpoint)
            my_endpoints.extend(self._session.settings.get("additional_endpoints", []))
        if hasattr(connection, "routing_keys"):
            did_doc = await self.create_routing_keys_did_document(
                did_info=my_info,
                recipient_keys=connection.recipient_keys,
                routing_keys=connection.routing_keys,
                endpoint=connection.endpoint  # does endpoint from invitation end up here?
            )
        else:
            did_doc = await self.create_did_document(
                my_info, connection.inbound_connection_id, my_endpoints
            )
        if not my_label:
            my_label = self._session.settings.get("default_label")
        request = ConnectionRequest(
            label=my_label,
            connection=ConnectionDetail(did=connection.my_did, did_doc=did_doc),
        )

        # Update connection state
        connection.request_id = request._id
        connection.state = ConnRecord.State.REQUEST.rfc160

        await connection.save(self._session, reason="Created connection request")

        return request

    async def receive_request(
        self, request: ConnectionRequest, receipt: MessageReceipt
    ) -> ConnRecord:
        """
        Receive and store a connection request.

        Args:
            request: The `ConnectionRequest` to accept
            receipt: The message receipt

        Returns:
            The new or updated `ConnRecord` instance

        """
        ConnRecord.log_state(
            self._session, "Receiving connection request", {"request": request}
        )

        connection = None
        connection_key = None

        # Determine what key will need to sign the response
        if receipt.recipient_did_public:
            wallet = self._session.inject(BaseWallet)
            my_info = await wallet.get_local_did(receipt.recipient_did)
            connection_key = my_info.verkey
        else:
            connection_key = receipt.recipient_verkey
            try:
                connection = await ConnRecord.retrieve_by_invitation_key(
                    session=self._session,
                    invitation_key=connection_key,
                    their_role=ConnRecord.Role.REQUESTER.rfc160,
                )
            except StorageNotFoundError:
                raise ConnectionManagerError(
                    "No invitation found for pairwise connection"
                )

        invitation = None
        if connection:
            invitation = await connection.retrieve_invitation(self._session)
            connection_key = connection.invitation_key
            ConnRecord.log_state(
                self._session, "Found invitation", {"invitation": invitation}
            )

            if connection.is_multiuse_invitation:
                wallet = self._session.inject(BaseWallet)
                my_info = await wallet.create_local_did()
                # update mediator keylist if mediation is set
                mediation_records = await MediationRecord.query(self._session)
                if hasattr(connection, "routing_keys") and \
                        self._session.settings.get(
                            "mediation.auto_send_keylist_update_in_requests"
                ) and mediation_records[0]:
                    # send a update keylist message with new recipient keys.
                    updates = [  # WARNING: possible race condition here
                        KeylistUpdateRule(
                            recipient_key=my_info.verkey,
                            action=KeylistUpdateRule.RULE_ADD
                        )
                    ]
                    responder = self._session.inject(BaseResponder, required=False)
                    update_keylist_request = KeylistUpdate(updates=updates)
                    # TODO: add config for active mediator to send recipient keys to.
                    await responder.send_reply(
                        update_keylist_request,
                        connection_id=mediation_records[0]
                    )
                new_connection = ConnRecord(
                    invitation_key=connection_key,
                    my_did=my_info.did,
                    state=ConnRecord.State.INVITATION.rfc160,
                    accept=connection.accept,
                    their_role=connection.their_role,
                )

                await new_connection.save(
                    self._session,
                    reason="Received connection request from multi-use invitation DID",
                )
                connection = new_connection

        conn_did_doc = request.connection.did_doc
        if not conn_did_doc:
            raise ConnectionManagerError(
                "No DIDDoc provided; cannot connect to public DID"
            )
        if request.connection.did != conn_did_doc.did:
            raise ConnectionManagerError(
                "Connection DID does not match DIDDoc id",
                error_code=ProblemReportReason.REQUEST_NOT_ACCEPTED,
            )
        await self.store_did_document(conn_did_doc)

        if connection:
            connection.their_label = request.label
            connection.their_did = request.connection.did
            connection.state = ConnRecord.State.REQUEST.rfc160
            await connection.save(
                self._session, reason="Received connection request from invitation"
            )
        elif not self._session.settings.get("public_invites"):
            raise ConnectionManagerError("Public invitations are not enabled")
        else:
            my_info = await wallet.create_local_did()
            # update mediator if mediation is set
            if hasattr(connection, "routing_keys") and self._session.settings.get(
                    "mediation.auto_send_keylist_update_in_requests"
            ):
                # send a update keylist message with new recipient keys.
                updates = [
                    KeylistUpdateRule(
                        recipient_key=my_info.verkey,
                        action=KeylistUpdateRule.RULE_ADD
                    )
                ]
                responder = self._session.inject(BaseResponder, required=False)
                update_keylist_request = KeylistUpdate(updates=updates)
                await responder.send_reply(update_keylist_request)
            connection = ConnRecord(
                invitation_key=connection_key,
                my_did=my_info.did,
                their_role=ConnRecord.Role.RESPONDER.rfc160,
                their_did=request.connection.did,
                their_label=request.label,
                state=ConnRecord.State.REQUEST.rfc160,
            )
            if self._session.settings.get("debug.auto_accept_requests"):
                connection.accept = ConnRecord.ACCEPT_AUTO

            await connection.save(
                self._session, reason="Received connection request from public DID"
            )

        # Attach the connection request so it can be found and responded to
        await connection.attach_request(self._session, request)

        if connection.accept == ConnRecord.ACCEPT_AUTO:
            response = await self.create_response(connection)
            responder = self._session.inject(BaseResponder, required=False)
            if responder:
                await responder.send_reply(
                    response, connection_id=connection.connection_id
                )
                # refetch connection for accurate state
                connection = await ConnRecord.retrieve_by_id(
                    self._session, connection.connection_id
                )
        else:
            self._logger.debug("Connection request will await acceptance")

        return connection

    async def create_response(
        self, connection: ConnRecord, my_endpoint: str = None
    ) -> ConnectionResponse:
        """
        Create a connection response for a received connection request.

        Args:
            connection: The `ConnRecord` with a pending connection request
            my_endpoint: The endpoint I can be reached at

        Returns:
            A tuple of the updated `ConnRecord` new `ConnectionResponse` message

        """
        ConnRecord.log_state(
            self._session,
            "Creating connection response",
            {"connection_id": connection.connection_id},
        )

        if ConnRecord.State.get(connection.state) not in (
            ConnRecord.State.REQUEST,
            ConnRecord.State.RESPONSE,
        ):
            raise ConnectionManagerError(
                "Connection is not in the request or response state"
            )

        request = await connection.retrieve_request(self._session)
        wallet = self._session.inject(BaseWallet)
        if connection.my_did:
            my_info = await wallet.get_local_did(connection.my_did)
        else:
            my_info = await wallet.create_local_did()
            connection.my_did = my_info.did

        # Create connection response message
        if my_endpoint:
            my_endpoints = [my_endpoint]
        else:
            my_endpoints = []
            default_endpoint = self._session.settings.get("default_endpoint")
            if default_endpoint:
                my_endpoints.append(default_endpoint)
            my_endpoints.extend(self._session.settings.get("additional_endpoints", []))
        did_doc = await self.create_did_document(
            my_info, connection.inbound_connection_id, my_endpoints
        )
        response = ConnectionResponse(
            connection=ConnectionDetail(did=my_info.did, did_doc=did_doc)
        )
        # Assign thread information
        response.assign_thread_from(request)
        response.assign_trace_from(request)
        # Sign connection field using the invitation key
        wallet = self._session.inject(BaseWallet)
        await response.sign_field("connection", connection.invitation_key, wallet)

        # Update connection state
        connection.state = ConnRecord.State.RESPONSE.rfc160

        await connection.save(
            self._session,
            reason="Created connection response",
            log_params={"response": response},
        )
        return response

    async def accept_response(
        self, response: ConnectionResponse, receipt: MessageReceipt
    ) -> ConnRecord:
        """
        Accept a connection response.

        Process a ConnectionResponse message by looking up
        the connection request and setting up the pairwise connection.

        Args:
            response: The `ConnectionResponse` to accept
            receipt: The message receipt

        Returns:
            The updated `ConnRecord` representing the connection

        Raises:
            ConnectionManagerError: If there is no DID associated with the
                connection response
            ConnectionManagerError: If the corresponding connection is not
                at the request or response stage

        """

        connection = None
        if response._thread:
            # identify the request by the thread ID
            try:
                connection = await ConnRecord.retrieve_by_request_id(
                    self._session, response._thread_id
                )
            except StorageNotFoundError:
                pass

        if not connection and receipt.sender_did:
            # identify connection by the DID they used for us
            try:
                connection = await ConnRecord.retrieve_by_did(
                    self._session, receipt.sender_did, receipt.recipient_did
                )
            except StorageNotFoundError:
                pass

        if not connection:
            raise ConnectionManagerError(
                "No corresponding connection request found",
                error_code=ProblemReportReason.RESPONSE_NOT_ACCEPTED,
            )

        if ConnRecord.State.get(connection.state) not in (
            ConnRecord.State.REQUEST,
            ConnRecord.State.RESPONSE,
        ):
            raise ConnectionManagerError(
                f"Cannot accept connection response for connection"
                f" in state: {connection.state}"
            )

        their_did = response.connection.did
        conn_did_doc = response.connection.did_doc
        if not conn_did_doc:
            raise ConnectionManagerError(
                "No DIDDoc provided; cannot connect to public DID"
            )
        if their_did != conn_did_doc.did:
            raise ConnectionManagerError("Connection DID does not match DIDDoc id")
        await self.store_did_document(conn_did_doc)

        connection.their_did = their_did
        connection.state = ConnRecord.State.RESPONSE.rfc160

        await connection.save(self._session, reason="Accepted connection response")

        return connection

    async def create_static_connection(
        self,
        my_did: str = None,
        my_seed: str = None,
        their_did: str = None,
        their_seed: str = None,
        their_verkey: str = None,
        their_endpoint: str = None,
        their_label: str = None,
        alias: str = None,
    ) -> (DIDInfo, DIDInfo, ConnRecord):
        """
        Register a new static connection (for use by the test suite).

        Args:
            my_did: override the DID used in the connection
            my_seed: provide a seed used to generate our DID and keys
            their_did: provide the DID used by the other party
            their_seed: provide a seed used to generate their DID and keys
            their_verkey: provide the verkey used by the other party
            their_endpoint: their URL endpoint for routing messages
            alias: an alias for this connection record

        Returns:
            Tuple: my DIDInfo, their DIDInfo, new `ConnRecord` instance

        """
        wallet = self._session.inject(BaseWallet)

        # seed and DID optional
        my_info = await wallet.create_local_did(my_seed, my_did)

        # must provide their DID and verkey if the seed is not known
        if (not their_did or not their_verkey) and not their_seed:
            raise ConnectionManagerError(
                "Either a verkey or seed must be provided for the other party"
            )
        if not their_did:
            their_did = seed_to_did(their_seed)
        if not their_verkey:
            their_verkey_bin, _ = create_keypair(their_seed.encode())
            their_verkey = bytes_to_b58(their_verkey_bin)
        their_info = DIDInfo(their_did, their_verkey, {})

        # Create connection record
        connection = ConnRecord(
            invitation_mode=ConnRecord.INVITATION_MODE_STATIC,
            my_did=my_info.did,
            their_did=their_info.did,
            their_label=their_label,
            state=ConnRecord.State.COMPLETED.rfc160,
            alias=alias,
        )
        await connection.save(self._session, reason="Created new static connection")

        # Synthesize their DID doc
        did_doc = await self.create_did_document(their_info, None, [their_endpoint])
        await self.store_did_document(did_doc)

        return my_info, their_info, connection

    async def find_connection(
        self,
        their_did: str,
        my_did: str = None,
        my_verkey: str = None,
        auto_complete=False,
    ) -> ConnRecord:
        """
        Look up existing connection information for a sender verkey.

        Args:
            their_did: Their DID
            my_did: My DID
            my_verkey: My verkey
            auto_complete: Should this connection automatically be promoted to active

        Returns:
            The located `ConnRecord`, if any

        """
        # self._log_state(
        #    "Finding connection",
        #    {"their_did": their_did, "my_did": my_did, "my_verkey": my_verkey},
        # )
        connection = None
        if their_did:
            try:
                connection = await ConnRecord.retrieve_by_did(
                    self._session, their_did, my_did
                )
            except StorageNotFoundError:
                pass

        if (
            connection
            and ConnRecord.State.get(connection.state) is ConnRecord.State.RESPONSE
            and auto_complete
        ):
            connection.state = ConnRecord.State.COMPLETED.rfc160
            await connection.save(self._session, reason="Connection promoted to active")

        if not connection and my_verkey:
            try:
                connection = await ConnRecord.retrieve_by_invitation_key(
                    self._session,
                    my_verkey,
                    their_role=ConnRecord.Role.REQUESTER.rfc160,
                )
            except StorageError:
                pass

        return connection

    async def find_inbound_connection(self, receipt: MessageReceipt) -> ConnRecord:
        """
        Deserialize an incoming message and further populate the request context.

        Args:
            receipt: The message receipt

        Returns:
            The `ConnRecord` associated with the expanded message, if any

        """

        cache_key = None
        connection = None
        resolved = False

        if receipt.sender_verkey and receipt.recipient_verkey:
            cache_key = (
                f"connection_by_verkey::{receipt.sender_verkey}"
                f"::{receipt.recipient_verkey}"
            )
            cache = self._session.inject(BaseCache, required=False)
            if cache:
                async with cache.acquire(cache_key) as entry:
                    if entry.result:
                        cached = entry.result
                        receipt.sender_did = cached["sender_did"]
                        receipt.recipient_did_public = cached["recipient_did_public"]
                        receipt.recipient_did = cached["recipient_did"]
                        connection = await ConnRecord.retrieve_by_id(
                            self._session, cached["id"]
                        )
                    else:
                        connection = await self.resolve_inbound_connection(receipt)
                        if connection:
                            cache_val = {
                                "id": connection.connection_id,
                                "sender_did": receipt.sender_did,
                                "recipient_did": receipt.recipient_did,
                                "recipient_did_public": receipt.recipient_did_public,
                            }
                            await entry.set_result(cache_val, 3600)
                        resolved = True

        if not connection and not resolved:
            connection = await self.resolve_inbound_connection(receipt)
        return connection

    async def resolve_inbound_connection(self, receipt: MessageReceipt) -> ConnRecord:
        """
        Populate the receipt DID information and find the related `ConnRecord`.

        Args:
            receipt: The message receipt

        Returns:
            The `ConnRecord` associated with the expanded message, if any

        """

        if receipt.sender_verkey:
            try:
                receipt.sender_did = await self.find_did_for_key(receipt.sender_verkey)
            except StorageNotFoundError:
                self._logger.warning(
                    "No corresponding DID found for sender verkey: %s",
                    receipt.sender_verkey,
                )

        if receipt.recipient_verkey:
            try:
                wallet = self._session.inject(BaseWallet)
                my_info = await wallet.get_local_did_for_verkey(
                    receipt.recipient_verkey
                )
                receipt.recipient_did = my_info.did
                if "public" in my_info.metadata and my_info.metadata["public"] is True:
                    receipt.recipient_did_public = True
            except InjectionError:
                self._logger.warning(
                    "Cannot resolve recipient verkey, no wallet defined by "
                    "context: %s",
                    receipt.recipient_verkey,
                )
            except WalletNotFoundError:
                self._logger.warning(
                    "No corresponding DID found for recipient verkey: %s",
                    receipt.recipient_verkey,
                )

        return await self.find_connection(
            receipt.sender_did, receipt.recipient_did, receipt.recipient_verkey, True
        )

    async def create_did_document(
        self,
        did_info: DIDInfo,
        inbound_connection_id: str = None,
        svc_endpoints: Sequence[str] = None,
    ) -> DIDDoc:
        """Create our DID document for a given DID.

        Args:
            did_info: The DID information (DID and verkey) used in the connection
            inbound_connection_id: The ID of the inbound routing connection to use
            svc_endpoints: Custom endpoints for the DID Document

        Returns:
            The prepared `DIDDoc` instance

        """

        did_doc = DIDDoc(did=did_info.did)
        did_controller = did_info.did
        did_key = did_info.verkey
        pk = PublicKey(
            did_info.did,
            "1",
            did_key,
            PublicKeyType.ED25519_SIG_2018,
            did_controller,
            True,
        )
        did_doc.set(pk)

        router_id = inbound_connection_id
        routing_keys = []
        router_idx = 1
        while router_id:
            # look up routing connection information
            router = await ConnRecord.retrieve_by_id(self._session, router_id)
            if ConnRecord.State.get(router.state) != ConnRecord.State.COMPLETED:
                raise ConnectionManagerError(
                    f"Router connection not active: {router_id}"
                )
            routing_doc, _ = await self.fetch_did_document(router.their_did)
            if not routing_doc.service:
                raise ConnectionManagerError(
                    f"No services defined by routing DIDDoc: {router_id}"
                )
            for service in routing_doc.service.values():
                if not service.endpoint:
                    raise ConnectionManagerError(
                        "Routing DIDDoc service has no service endpoint"
                    )
                if not service.recip_keys:
                    raise ConnectionManagerError(
                        "Routing DIDDoc service has no recipient key(s)"
                    )
                rk = PublicKey(
                    did_info.did,
                    f"routing-{router_idx}",
                    service.recip_keys[0].value,
                    PublicKeyType.ED25519_SIG_2018,
                    did_controller,
                    True,
                )
                routing_keys.append(rk)
                svc_endpoints = [service.endpoint]
                break
            router_id = router.inbound_connection_id

        for endpoint_index, svc_endpoint in enumerate(svc_endpoints or []):
            endpoint_ident = "indy" if endpoint_index == 0 else f"indy{endpoint_index}"
            service = Service(
                did_info.did,
                endpoint_ident,
                "IndyAgent",
                [pk],
                routing_keys,
                svc_endpoint,
            )
            did_doc.set(service)

        return did_doc

    async def create_routing_keys_did_document(
        self,
        did_info: DIDInfo,
        recipient_keys: Sequence[str] = None,
        routing_keys: Sequence[str] = None,
        endpoint: str = None,
    ) -> DIDDoc:
        """Create DID document for connection request with routing keys.

        Args:

        Returns:
            The prepared `DIDDoc` instance

        """
        recip_keys = []
        route_keys = []
        did_doc = DIDDoc(did=did_info.did)
        wallet: BaseWallet = self._session.inject(BaseWallet)
        for recipient_key in recipient_keys:
            # TODO: resolve (get did record for verkey)
            # TODO wrap in try/except
            did_info = await wallet.get_local_did_for_verkey(recipient_key)
            recip_key = PublicKey(
                did_info.did,
                "1",  # TODO: why one?
                did_info.verkey,
                PublicKeyType.ED25519_SIG_2018,
                did_info.did,
                True,
            )
            recip_keys.append(recip_key)

        for routing_key in routing_keys:
            # TODO: resolve did_dock for provided verkey
            route_key = PublicKey(
                did="55GkHamhTU1ZbTbV2ab9DE",  # fake! TODO: not provided from mediator
                ident="1",  # f"routing-{connection.id}",
                value=routing_key,
                pk_type=PublicKeyType.ED25519_SIG_2018,
                controller="55GkHamhTU1ZbTbV2ab9DE",  # fake! TODO: this is never provided
                authn=False,
            )
            route_keys.append(route_key)

        endpoint_ident = "indy"
        service = Service(
            did=did_info.did,
            ident=endpoint_ident,
            typ="IndyAgent",
            recip_keys=recip_keys,
            routing_keys=route_keys,
            endpoint=endpoint,
        )
        did_doc.set(service)

        return did_doc

    async def fetch_did_document(self, did: str) -> Tuple[DIDDoc, StorageRecord]:
        """Retrieve a DID Document for a given DID.

        Args:
            did: The DID to search for
        """
        storage = self._session.inject(BaseStorage)
        record = await storage.find_record(self.RECORD_TYPE_DID_DOC, {"did": did})
        return DIDDoc.from_json(record.value), record

    async def store_did_document(self, did_doc: DIDDoc):
        """Store a DID document.

        Args:
            did_doc: The `DIDDoc` instance to be persisted
        """
        assert did_doc.did
        storage: BaseStorage = self._session.inject(BaseStorage)
        try:
            stored_doc, record = await self.fetch_did_document(did_doc.did)
        except StorageNotFoundError:
            record = StorageRecord(
                self.RECORD_TYPE_DID_DOC, did_doc.to_json(), {"did": did_doc.did}
            )
            await storage.add_record(record)
        else:
            await storage.update_record(record, did_doc.to_json(), {"did": did_doc.did})
        await self.remove_keys_for_did(did_doc.did)
        for key in did_doc.pubkey.values():
            if key.controller == did_doc.did:
                await self.add_key_for_did(did_doc.did, key.value)

    async def add_key_for_did(self, did: str, key: str):
        """Store a verkey for lookup against a DID.

        Args:
            did: The DID to associate with this key
            key: The verkey to be added
        """
        record = StorageRecord(self.RECORD_TYPE_DID_KEY, key, {"did": did, "key": key})
        storage = self._session.inject(BaseStorage)
        await storage.add_record(record)

    async def find_did_for_key(self, key: str) -> str:
        """Find the DID previously associated with a key.

        Args:
            key: The verkey to look up
        """
        storage = self._session.inject(BaseStorage)
        record = await storage.find_record(self.RECORD_TYPE_DID_KEY, {"key": key})
        return record.tags["did"]

    async def remove_keys_for_did(self, did: str):
        """Remove all keys associated with a DID.

        Args:
            did: The DID to remove keys for
        """
        storage = self._session.inject(BaseStorage)
        await storage.delete_all_records(self.RECORD_TYPE_DID_KEY, {"did": did})

    async def get_connection_targets(
        self, *, connection_id: str = None, connection: ConnRecord = None
    ):
        """Create a connection target from a `ConnRecord`.

        Args:
            connection_id: The connection ID to search for
            connection: The connection record itself, if already available
        """
        if not connection_id:
            connection_id = connection.connection_id
        cache = self._session.inject(BaseCache, required=False)
        cache_key = f"connection_target::{connection_id}"
        if cache:
            async with cache.acquire(cache_key) as entry:
                if entry.result:
                    targets = [
                        ConnectionTarget.deserialize(row) for row in entry.result
                    ]
                else:
                    if not connection:
                        connection = await ConnRecord.retrieve_by_id(
                            self._session, connection_id
                        )
                    targets = await self.fetch_connection_targets(connection)
                    await entry.set_result([row.serialize() for row in targets], 3600)
        else:
            targets = await self.fetch_connection_targets(connection)
        return targets

    async def fetch_connection_targets(
        self, connection: ConnRecord
    ) -> Sequence[ConnectionTarget]:
        """Get a list of connection target from a `ConnRecord`.

        Args:
            connection: The connection record (with associated `DIDDoc`)
                used to generate the connection target
        """

        if not connection.my_did:
            self._logger.debug("No local DID associated with connection")
            return None

        wallet = self._session.inject(BaseWallet)
        my_info = await wallet.get_local_did(connection.my_did)
        results = None

        if (
            ConnRecord.State.get(connection.state)
            in (ConnRecord.State.INVITATION, ConnRecord.State.REQUEST)
            and ConnRecord.Role.get(connection.their_role) is ConnRecord.Role.RESPONDER
        ):
            invitation = await connection.retrieve_invitation(self._session)
            if isinstance(invitation, ConnectionInvitation):  # conn protocol invitation
                if invitation.did:
                    # populate recipient keys and endpoint from the ledger
                    ledger = self._session.inject(BaseLedger, required=False)
                    if not ledger:
                        raise ConnectionManagerError(
                            "Cannot resolve DID without ledger instance"
                        )
                    async with ledger:
                        endpoint = await ledger.get_endpoint_for_did(invitation.did)
                        recipient_keys = [await ledger.get_key_for_did(invitation.did)]
                        routing_keys = []
                else:
                    endpoint = invitation.endpoint
                    recipient_keys = invitation.recipient_keys
                    routing_keys = invitation.routing_keys
            else:  # out-of-band invitation
                if invitation.service_dids:
                    # populate recipient keys and endpoint from the ledger
                    ledger = self._session.inject(BaseLedger, required=False)
                    if not ledger:
                        raise ConnectionManagerError(
                            "Cannot resolve DID without ledger instance"
                        )
                    async with ledger:
                        endpoint = await ledger.get_endpoint_for_did(
                            invitation.service_dids[0]
                        )
                        recipient_keys = [
                            await ledger.get_key_for_did(invitation.service_dids[0])
                        ]
                        routing_keys = []
                else:
                    endpoint = invitation.service_blocks[0].service_endpoint
                    recipient_keys = [
                        did_key_to_naked(k)
                        for k in invitation.service_blocks[0].recipient_keys
                    ]
                    routing_keys = [
                        did_key_to_naked(k)
                        for k in invitation.service_blocks[0].routing_keys
                    ]

            results = [
                ConnectionTarget(
                    did=connection.their_did,
                    endpoint=endpoint,
                    label=invitation.label,
                    recipient_keys=recipient_keys,
                    routing_keys=routing_keys,
                    sender_key=my_info.verkey,
                )
            ]
        else:
            if not connection.their_did:
                self._logger.debug("No target DID associated with connection")
                return None

            did_doc, _ = await self.fetch_did_document(connection.their_did)
            results = self.diddoc_connection_targets(
                did_doc, my_info.verkey, connection.their_label
            )

        return results

    def diddoc_connection_targets(
        self, doc: DIDDoc, sender_verkey: str, their_label: str = None
    ) -> Sequence[ConnectionTarget]:
        """Get a list of connection targets from a DID Document.

        Args:
            doc: The DID Document to create the target from
            sender_verkey: The verkey we are using
            their_label: The connection label they are using
        """

        if not doc:
            raise ConnectionManagerError("No DIDDoc provided for connection target")
        if not doc.did:
            raise ConnectionManagerError("DIDDoc has no DID")
        if not doc.service:
            raise ConnectionManagerError("No services defined by DIDDoc")

        targets = []
        for service in doc.service.values():
            if service.recip_keys:
                targets.append(
                    ConnectionTarget(
                        did=doc.did,
                        endpoint=service.endpoint,
                        label=their_label,
                        recipient_keys=[
                            key.value for key in (service.recip_keys or ())
                        ],
                        routing_keys=[
                            key.value for key in (service.routing_keys or ())
                        ],
                        sender_key=sender_verkey,
                    )
                )
        return targets

    async def establish_inbound(
        self,
        connection: ConnRecord,
        inbound_connection_id: str,
        outbound_handler: Coroutine,
    ) -> str:
        """Assign the inbound routing connection for a connection record.

        Returns: the current routing state (request or done)

        """

        # The connection must have a verkey, but in the case of a received
        # invitation we might not have created one yet
        wallet = self._session.inject(BaseWallet)
        if connection.my_did:
            my_info = await wallet.get_local_did(connection.my_did)
        else:
            # Create new DID for connection
            my_info = await wallet.create_local_did()
            connection.my_did = my_info.did

        try:
            router = await ConnRecord.retrieve_by_id(
                self._session, inbound_connection_id
            )
        except StorageNotFoundError:
            raise ConnectionManagerError(
                f"Routing connection not found: {inbound_connection_id}"
            )
        if not router.is_ready:
            raise ConnectionManagerError(
                f"Routing connection is not ready: {inbound_connection_id}"
            )
        connection.inbound_connection_id = inbound_connection_id

        route_mgr = RoutingManager(self._session)

        await route_mgr.send_create_route(
            inbound_connection_id, my_info.verkey, outbound_handler
        )
        connection.routing_state = ConnRecord.ROUTING_STATE_REQUEST
        await connection.save(self._session)
        return connection.routing_state

    async def update_inbound(
        self, inbound_connection_id: str, recip_verkey: str, routing_state: str
    ):
        """Activate connections once a route has been established.

        Looks up pending connections associated with the inbound routing
        connection and marks the routing as complete.
        """
        conns = await ConnRecord.query(
            self._session, {"inbound_connection_id": inbound_connection_id}
        )
        wallet = self._session.inject(BaseWallet)

        for connection in conns:
            # check the recipient key
            if not connection.my_did:
                continue
            conn_info = await wallet.get_local_did(connection.my_did)
            if conn_info.verkey == recip_verkey:
                connection.routing_state = routing_state
                await connection.save(self._session)
