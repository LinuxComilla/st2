import datetime
import json
import jsonschema
import pecan
from pecan import abort
from pecan.rest import RestController
import six

from st2common import log as logging
from st2common.models.base import jsexpose
from st2common.persistence.action import ActionExecution
from st2common.models.api.action import (ActionExecutionAPI,
                                         ACTIONEXEC_STATUS_INIT,
                                         ACTIONEXEC_STATUS_SCHEDULED)
from st2common.util import schema as util_schema
from st2common.util.action_db import (get_action_by_dict, update_actionexecution_status,
                                      get_runnertype_by_name)

http_client = six.moves.http_client

LOG = logging.getLogger(__name__)


MONITOR_THREAD_EMPTY_Q_SLEEP_TIME = 5
MONITOR_THREAD_NO_WORKERS_SLEEP_TIME = 1


class ActionExecutionsController(RestController):
    """
        Implements the RESTful web endpoint that handles
        the lifecycle of ActionExecutions in the system.
    """

    @staticmethod
    def __get_by_id(id):
        try:
            return ActionExecution.get_by_id(id)
        except Exception as e:
            msg = 'Database lookup for id="%s" resulted in exception. %s' % (id, e)
            LOG.exception(msg)
            abort(http_client.NOT_FOUND, msg)

    @staticmethod
    def _get_action_executions(action_id, action_name, limit=None, **kw):
        if action_id is not None:
            LOG.debug('Using action_id=%s to get action executions', action_id)
            # action__id <- this queries action.id
            return ActionExecution.query(action__id=action_id,
                                         order_by=['-start_timestamp'],
                                         limit=limit, **kw)
        elif action_name is not None:
            LOG.debug('Using action_name=%s to get action executions', action_name)
            # action__name <- this queries against action.name
            return ActionExecution.query(action__name=action_name,
                                         order_by=['-start_timestamp'],
                                         limit=limit, **kw)
        LOG.debug('Retrieving all action executions')
        return ActionExecution.get_all(order_by=['-start_timestamp'],
                                       limit=limit, **kw)

    def _create_liveaction_data(self, actionexecution_id):
        return {'actionexecution_id': str(actionexecution_id)}

    @jsexpose(str)
    def get_one(self, id):
        """
            List actionexecution by id.

            Handle:
                GET /actionexecutions/1
        """
        LOG.info('GET /actionexecutions/ with id=%s', id)
        actionexec_db = ActionExecutionsController.__get_by_id(id)
        actionexec_api = ActionExecutionAPI.from_model(actionexec_db)
        LOG.debug('GET /actionexecutions/ with id=%s, client_result=%s', id, actionexec_api)
        return actionexec_api

    @jsexpose(str, str, str)
    def get_all(self, action_id=None, action_name=None, limit='50', **kw):
        """
            List all actionexecutions.

            Handles requests:
                GET /actionexecutions/
        """

        LOG.info('GET all /actionexecutions/ with action_name=%s, '
                 'action_id=%s, and limit=%s', action_name, action_id, limit)

        actionexec_dbs = ActionExecutionsController._get_action_executions(
            action_id, action_name, limit=int(limit), **kw)
        actionexec_apis = [ActionExecutionAPI.from_model(actionexec_db)
                           for actionexec_db
                           in sorted(actionexec_dbs,
                                     key=lambda x: x.start_timestamp)]

        # TODO: unpack list in log message
        LOG.debug('GET all /actionexecutions/ client_result=%s', actionexec_apis)
        return actionexec_apis

    @jsexpose(body=ActionExecutionAPI, status_code=http_client.CREATED)
    def post(self, actionexecution):
        """
            Create a new actionexecution.

            Handles requests:
                POST /actionexecutions/
        """

        LOG.info('POST /actionexecutions/ with actionexec data=%s', actionexecution)

        actionexecution.start_timestamp = datetime.datetime.now()

        # Retrieve context from request header.
        if ('st2-context' in pecan.request.headers and pecan.request.headers['st2-context']):
            context = pecan.request.headers['st2-context'].replace("'", "\"")
            actionexecution.context = json.loads(context)

        # Fill-in runner_parameters and action_parameter fields if they are not
        # provided in the request.
        if not hasattr(actionexecution, 'parameters'):
            LOG.warning('POST /actionexecutions/ request did not '
                        'provide parameters field.')
            setattr(actionexecution, 'runner_parameters', {})

        (action_db, action_dict) = get_action_by_dict(actionexecution.action)
        LOG.debug('POST /actionexecutions/ Action=%s', action_db)

        if not action_db:
            LOG.error('POST /actionexecutions/ Action for "%s" cannot be found.',
                      actionexecution.action)
            abort(http_client.NOT_FOUND, 'Unable to find action.')
            return

        actionexecution.action = action_dict

        # If the Action is disabled, abort the POST call.
        if not action_db.enabled:
            LOG.error('POST /actionexecutions/ Unable to create Action Execution for a disabled '
                      'Action. Action is: %s', action_db)
            abort(http_client.FORBIDDEN, 'Action is disabled.')
            return

        # Assign default parameters
        runnertype = get_runnertype_by_name(action_db.runner_type['name'])
        LOG.debug('POST /actionexecutions/ Runner=%s', runnertype)
        for key, metadata in six.iteritems(runnertype.runner_parameters):
            if key not in actionexecution.parameters and 'default' in metadata:
                if metadata.get('default') is not None:
                    actionexecution.parameters[key] = metadata['default']

        # Validate action parameters
        schema = util_schema.get_parameter_schema(action_db)
        try:
            LOG.debug('POST /actionexecutions/ Validation for parameters=%s & schema=%s',
                      actionexecution.parameters, schema)
            jsonschema.validate(actionexecution.parameters, schema)
            LOG.debug('POST /actionexecutions/ Parameter validation passed.')
        except jsonschema.ValidationError as e:
            LOG.error('POST /actionexecutions/ Parameter validation failed. %s', actionexecution)
            abort(http_client.BAD_REQUEST, str(e))
            return

        # Set initial value for ActionExecution status.
        # Not using update_actionexecution_status to allow other initialization to
        # be done before saving to DB.
        actionexecution.status = ACTIONEXEC_STATUS_INIT
        actionexec_db = ActionExecutionAPI.to_model(actionexecution)
        actionexec_db = ActionExecution.add_or_update(actionexec_db)
        LOG.audit('ActionExecution created. ActionExecution=%s. ', actionexec_db)
        actionexec_id = actionexec_db.id
        actionexec_status = ACTIONEXEC_STATUS_SCHEDULED
        actionexec_db = update_actionexecution_status(actionexec_status,
                                                      actionexec_id=actionexec_id)
        actionexec_api = ActionExecutionAPI.from_model(actionexec_db)
        LOG.debug('POST /actionexecutions/ client_result=%s', actionexec_api)
        return actionexec_api

    @jsexpose(str, body=ActionExecutionAPI)
    def put(self, id, actionexecution):
        actionexecution.start_timestamp = datetime.datetime.now()
        actionexec_db = ActionExecutionsController.__get_by_id(id)
        new_actionexec_db = ActionExecutionAPI.to_model(actionexecution)
        if actionexec_db.status != new_actionexec_db.status:
            actionexec_db.status = new_actionexec_db.status
        if actionexec_db.result != new_actionexec_db.result:
            actionexec_db.result = new_actionexec_db.result
        actionexec_db = ActionExecution.add_or_update(actionexec_db)
        actionexec_api = ActionExecutionAPI.from_model(actionexec_db)
        return actionexec_api