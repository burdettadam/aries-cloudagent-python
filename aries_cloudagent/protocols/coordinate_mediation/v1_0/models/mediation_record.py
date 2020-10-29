"""Store state for Mediation requests."""

from typing import Sequence

from marshmallow import EXCLUDE, fields, validate

from .....config.injection_context import InjectionContext

from .....messaging.models.base_record import BaseRecord, BaseRecordSchema
from .....messaging.models.openapi import OpenAPISchema
from .....messaging.valid import UUIDFour


class MediationRecord(BaseRecord):
    """Class representing stored route information.

    Args:
        connection id:
        terms:
    """

    class Meta:
        """RouteRecord metadata."""

        schema_class = "MediationRecordSchema"

    RECORD_TYPE = "mediation_requests"
    RECORD_ID_NAME = "mediation_id"
    TAG_NAMES = {"state", "connection_id"}

    STATE_REQUEST_RECEIVED = "request_received"
    STATE_GRANTED = "granted"
    STATE_DENIED = "denied"

    ROLE_CLIENT = "client"
    ROLE_SERVER = "server"

    def __init__(
        self,
        *,
        mediation_id: str = None,
        state: str = None,
        role: str = None,
        connection_id: str = None,
        mediator_terms: Sequence[str] = None,
        recipient_terms: Sequence[str] = None,
        **kwargs
    ):
        """
        Initialize a MediationRecord instance.

        Args:
            mediation_id:
            state:
            connection_id:
            terms:
        """
        super().__init__(
            mediation_id, state or self.STATE_REQUEST_RECEIVED, **kwargs
        )
        self.connection_id = connection_id
        self.mediator_terms = list(mediator_terms) if mediator_terms else []
        self.recipient_terms = list(recipient_terms) if recipient_terms else []

    @property
    def mediation_id(self) -> str:
        """Get Mediation ID."""
        return self._id

    @classmethod
    async def retrieve_by_connection_id(
        cls, context: InjectionContext, connection_id: str
    ):
        """Retrieve a route record by recipient key."""
        tag_filter = {"connection_id": connection_id}
        # TODO post filter out our mediation requests?
        return await cls.retrieve_by_tag_filter(context, tag_filter)


class MediationRecordSchema(BaseRecordSchema):
    """MediationRecordSchema schema."""

    class Meta:
        """MediationRecordSchema metadata."""

        model_class = MediationRecord
        unknown = EXCLUDE

    mediation_id = fields.Str(required=False)
    connection_id = fields.Str(required=True)
    mediator_terms = fields.List(fields.Str(), required=False)
    recipient_terms = fields.List(fields.Str(), required=False)


MEDIATION_STATE_SCHEMA = fields.Str(
    description="Mediation state (optional)",
    required=False,
    validate=validate.OneOf(
        [
                getattr(MediationRecord, m)
                for m in vars(MediationRecord)
                if m.startswith("STATE_")
        ]
    ),
    example="'request_received',"
    "'granted' or 'denied'",
)


MEDIATION_ID_SCHEMA = {
    "validate": UUIDFour(),
    "example": UUIDFour.EXAMPLE
}  # TODO: is mediation req id a did?


CONNECTION_ID_SCHEMA = fields.UUID(  # TODO: move this into connections.
    description="Connection identifier (optional)",
    required=False,
    example=UUIDFour.EXAMPLE,  # typically but not necessarily a UUID4
)


MEDIATOR_TERMS_SCHEMA = fields.List(
    fields.Str(
        description="Indicate terms that the mediator "
        "requires the recipient to agree to"
    ),
    required=False,
    description="List of mediator rules for recipient",
)


RECIPIENT_TERMS_SCHEMA = fields.List(
    fields.Str(
        description="Indicate terms that the recipient "
        "requires the mediator to agree to"
    ),
    required=False,
    description="List of recipient rules for mediation",
)


ENDPOINT_SCHEMA = fields.Str(
    description="endpoint on which messages destined "
    "for the recipient are received.",
    example="http://192.168.56.102:8020/"
)


ROUTING_KEYS_SCHEMA = fields.List(
    fields.Str(
        description="Keys to use for forward message packaging"
    ),
    required=False,
)


class MediationRecordReportSchema(OpenAPISchema):
    """MediationRecordSchema schema."""

    mediation_id = MEDIATION_ID_SCHEMA
    conn_id = CONNECTION_ID_SCHEMA
    mediator_terms = MEDIATOR_TERMS_SCHEMA
    recipient_terms = RECIPIENT_TERMS_SCHEMA
    endpoint = ENDPOINT_SCHEMA
    routing_keys = ROUTING_KEYS_SCHEMA
