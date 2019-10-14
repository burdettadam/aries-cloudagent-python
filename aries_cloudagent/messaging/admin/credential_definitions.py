"""Define messages for credential definitions admin protocols."""

# pylint: disable=invalid-name
# pylint: disable=too-few-public-methods

from asyncio import shield

from marshmallow import fields

from . import generate_model_schema, admin_only
from ..base_handler import BaseHandler, BaseResponder, RequestContext
from ...ledger.base import BaseLedger
from ..models.base_record import BaseRecord, BaseRecordSchema

from ..problem_report.message import ProblemReport


PROTOCOL = 'did:sov:BzCbsNYhMrjHiqZDTUASHg;spec/admin-credential-definitions/1.0'

SEND_CRED_DEF = '{}/send-credential-definition'.format(PROTOCOL)
CRED_DEF_ID = '{}/credential-definition-id'.format(PROTOCOL)
CRED_DEF_GET = '{}/credential-definition-get'.format(PROTOCOL)
CRED_DEF = '{}/credential-definition'.format(PROTOCOL)
CRED_DEF_GET_LIST = '{}/credential-definition-get-list'.format(PROTOCOL)
CRED_DEF_LIST = '{}/credential-definition-list'.format(PROTOCOL)

MESSAGE_TYPES = {
    SEND_CRED_DEF:
        'aries_cloudagent.messaging.admin.credential_definitions.SendCredDef',
    CRED_DEF_ID:
        'aries_cloudagent.messaging.admin.credential_definitions.CredDefID',
    CRED_DEF_GET:
        'aries_cloudagent.messaging.admin.credential_definitions.CredDefGet',
    CRED_DEF:
        'aries_cloudagent.messaging.admin.credential_definitions.CredDef',
    CRED_DEF_GET_LIST:
        'aries_cloudagent.messaging.admin.credential_definitions.CredDefGetList',
    CRED_DEF_LIST:
        'aries_cloudagent.messaging.admin.credential_definitions.CredDefList',
}


class CredDefRecord(BaseRecord):
    """Represents a Schema."""

    RECORD_ID_NAME = "record_id"
    RECORD_TYPE = "cred_def"

    STATE_UNWRITTEN = "unwritten"
    STATE_WRITTEN = "written"

    class Meta:
        """CredDefRecord metadata."""

        schema_class = "CredDefRecordSchema"

    def __init__(
            self,
            *,
            record_id: str = None,
            cred_def_id: str = None,
            cred_def_json: str = None,
            schema_id: str = None,
            state: str = None,
            **kwargs):
        """Initialize a new SchemaRecord."""
        super().__init__(record_id, state or self.STATE_UNWRITTEN, **kwargs)
        self.cred_def_id = cred_def_id
        self.cred_def_json = cred_def_json
        self.schema_id = schema_id

    @property
    def record_id(self) -> str:
        """Accessor for this schema's id."""
        return self._id

    @property
    def record_value(self) -> dict:
        """Get record value."""
        return {}

    @property
    def record_tags(self) -> dict:
        """Get tags for record."""
        return {
            prop: getattr(self, prop)
            for prop in (
                'cred_def_id',
                'cred_def_json',
                'schema_id',
                'state',
            )
        }


class CredDefRecordSchema(BaseRecordSchema):
    """Schema to allow serialization/deserialization of Schema records."""

    class Meta:
        """PoolRecordSchema metadata."""

        model_class = CredDefRecord

    cred_def_id = fields.Str(required=False)
    cred_def_json = fields.Str(required=False)
    schema_id = fields.Str(required=False)


SendCredDef, SendCredDefSchema = generate_model_schema(
    name='SendCredDef',
    handler='aries_cloudagent.messaging.admin.credential_definitions'
            '.SendCredDefHandler',
    msg_type=SEND_CRED_DEF,
    schema={
        'schema_id': fields.Str(required=True)
    }
)

CredDefID, CredDefIDSchema = generate_model_schema(
    name='CredDefID',
    handler='aries_cloudagent.messaging.admin.PassHandler',
    msg_type=CRED_DEF_ID,
    schema={
        'cred_def_id': fields.Str(required=True),
        'cred_def_json': fields.Str(required=True)
    }
)


class SendCredDefHandler(BaseHandler):
    """Handler for received send cred def request."""

    @admin_only
    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Handle received send cred def request."""
        ledger: BaseLedger = await context.inject(BaseLedger)
        try:
            async with ledger:
                credential_definition_id, credential_definition_json = await shield(
                    ledger.send_credential_definition(context.message.schema_id)
                )
        except Exception as err:
            report = ProblemReport(
                explain_ltxt='Failed to send to ledger; Error: {}'.format(err),
                who_retries='none'
            )
            await responder.send_reply(report)
            return

        cred_def_record = CredDefRecord(
            cred_def_id=credential_definition_id,
            cred_def_json=credential_definition_json,
            schema_id=context.message.schema_id,
            state=CredDefRecord.STATE_WRITTEN
        )
        await cred_def_record.save(
            context,
            reason="Committed credential definition to ledger"
        )

        result = CredDefID(
            cred_def_id=credential_definition_id,
            cred_def_json=credential_definition_json,
        )
        result.assign_thread_from(context.message)
        await responder.send_reply(result)


CredDefGet, CredDefGetSchema = generate_model_schema(
    name="CredDefGet",
    handler='aries_cloudagent.messaging.admin.credential_definitions'
            '.CredDefGetHandler',
    msg_type=CRED_DEF_GET,
    schema={
        'cred_def_id': fields.Str(required=True)
    }
)

CredDef, CredDefSchema = generate_model_schema(
    name="CredDef",
    handler='aries_cloudagent.messaging.admin.PassHandler',
    msg_type=CRED_DEF,
    schema={
        'credential_definition': fields.Dict()
    }
)


class CredDefGetHandler(BaseHandler):
    """Handler for cred def get requests."""

    @admin_only
    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Handle received cred def get requests."""

        ledger: BaseLedger = await context.inject(BaseLedger)
        async with ledger:
            credential_definition = await ledger.get_credential_definition(
                context.message.cred_def_id
            )
        cred_def = CredDef(credential_definition=credential_definition)
        cred_def.assign_thread_from(context.message)
        await responder.send_reply(cred_def)


CredDefGetList, CredDefGetListSchema = generate_model_schema(
    name='CredDefGetList',
    handler='aries_cloudagent.messaging.admin.credential_definitions'
            '.CredDefGetListHandler',
    msg_type=CRED_DEF_GET_LIST,
    schema={
    }
)

CredDefList, CredDefListSchema = generate_model_schema(
    name='CredDefList',
    handler='aries_cloudagent.messaging.admin.PassHandler',
    msg_type=CRED_DEF_LIST,
    schema={
        'results': fields.List(
            fields.Nested(CredDefRecordSchema),
            required=True
        )
    }
)


class CredDefGetListHandler(BaseHandler):
    """Handler for get schema list request."""

    @admin_only
    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Handle get schema list request."""
        records = await CredDefRecord.query(context, {})
        cred_def_list = CredDefList(results=records)
        cred_def_list.assign_thread_from(context.message)
        await responder.send_reply(cred_def_list)
