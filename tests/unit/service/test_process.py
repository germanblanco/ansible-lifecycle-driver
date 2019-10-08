import unittest
import uuid
import json
import logging
import sys
import time
from unittest.mock import patch, MagicMock, ANY, DEFAULT
from ignition.boot.config import BootstrapApplicationConfiguration, PropertyGroups
from ignition.model.lifecycle import LifecycleExecuteResponse, LifecycleExecution, STATUS_COMPLETE, STATUS_FAILED, STATUS_IN_PROGRESS
from ignition.model.failure import FailureDetails, FAILURE_CODE_INFRASTRUCTURE_ERROR, FAILURE_CODE_INTERNAL_ERROR, FAILURE_CODE_RESOURCE_NOT_FOUND, FAILURE_CODE_INSUFFICIENT_CAPACITY
from ansibledriver.service.cache import CacheProperties
from ansibledriver.service.queue import RequestQueue
from ansibledriver.service.process import AnsibleProcessorService, ProcessProperties
from ansibledriver.service.ansible import AnsibleProperties
from ignition.utils.file import DirectoryTree

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler(sys.stdout)
logger.addHandler(stream_handler)

def sleep(request_id, *args, **kwargs):
  logger.info('sleeping for request {0}...'.format(request_id))
  time.sleep(1)
  return LifecycleExecution(request_id, STATUS_COMPLETE, None, {})

class LifecycleExecutionMatcher:
  def __init__(self, expected):
    self.expected = expected

  def compare(self, other):
    if not type(self.expected) == type(other):
      return False
    if other.status != self.expected.status:
      return False
    if other.request_id != self.expected.request_id:
      return False
    if len(self.expected.outputs.items() - other.outputs.items()) > 0:
      return False
    if self.expected.failure_details is not None:
        if other.failure_details is None:
            return False
        if other.failure_details.failure_code != self.expected.failure_details.failure_code:
          return False
        if other.failure_details.description != self.expected.failure_details.description:
          return False

    return True

  # "other" is the actual argument, to be compared against self.expected
  def __eq__(self, other):
    return self.compare(other)

class TestProcess(unittest.TestCase):

    def setUp(self):
        self.request_queue = RequestQueue()
        self.mock_ansible_client = MagicMock()
        self.mock_messaging_service = MagicMock()
        property_groups = PropertyGroups()
        property_groups.add_property_group(AnsibleProperties())
        property_groups.add_property_group(ProcessProperties())
        self.configuration = BootstrapApplicationConfiguration(app_name='test', property_sources=[], property_groups=property_groups, service_configurators=[], api_configurators=[], api_error_converter=None)
        self.ansible_processor = AnsibleProcessorService(self.configuration, self.request_queue, self.mock_ansible_client, messaging_service=self.mock_messaging_service)

    def tearDown(self):
        self.ansible_processor.shutdown()

    def assertLifecycleExecutionEqual(self, resp, expected_resp):
        self.assertEqual(resp.status, expected_resp.status)
        self.assertEqual(resp.outputs, expected_resp.outputs)
        self.assertEqual(resp.request_id, expected_resp.request_id)
        if resp.failure_details is not None:
            if expected_resp.failure_details is None:
                self.fail('Expected failure_details to be non-null')
            self.assertEqual(resp.failure_details.failure_code, expected_resp.failure_details.failure_code)
            self.assertEqual(resp.failure_details.description, expected_resp.failure_details.description)

    def check_response(self, lifecycle_execution):
      for i in range(10):
        call_count = self.mock_messaging_service.send_lifecycle_execution.call_count
        if call_count > 0:
          self.mock_messaging_service.send_lifecycle_execution.assert_called_once_with(LifecycleExecutionMatcher(lifecycle_execution))
          break
        else:
          time.sleep(1)
      else:
        self.fail('Timeout waiting for response')

    def test_run_lifecycle_invalid_request(self):
        with self.assertRaises(ValueError) as context:
          self.ansible_processor.run_lifecycle({
            'lifecycle_name': 'install',
            'lifecycle_path': DirectoryTree('./'),
            'system_properties': {
            },
            'properties': {
            },
            'deployment_location': {
            }
          })
        self.assertEqual(str(context.exception), 'Request must have a request_id')

        with self.assertRaises(ValueError) as context:
          self.ansible_processor.run_lifecycle({
            'request_id': uuid.uuid4().hex,
            'lifecycle_path': DirectoryTree('./'),
            'system_properties': {
            },
            'properties': {
            },
            'deployment_location': {
            }
          })
        self.assertEqual(str(context.exception), 'Request must have a lifecycle_name')

        with self.assertRaises(ValueError) as context:
          self.ansible_processor.run_lifecycle({
            'request_id': uuid.uuid4().hex,
            'lifecycle_name': 'install',
            'system_properties': {
            },
            'properties': {
            },
            'deployment_location': {
            }
          })
        self.assertEqual(str(context.exception), 'Request must have a lifecycle_path')

    def test_run_lifecycle(self):
        request_id = uuid.uuid4().hex

        lifecycle_execution = LifecycleExecution(request_id, STATUS_COMPLETE, None, {
          'prop1': 'output__value1'
        })
        self.mock_ansible_client.run_lifecycle_playbook.return_value = lifecycle_execution

        self.ansible_processor.run_lifecycle({
          'lifecycle_name': 'install',
          'lifecycle_path': DirectoryTree('./'),
          'system_properties': {
          },
          'properties': {
          },
          'deployment_location': {
          },
          'request_id': request_id
        })

        self.check_response(lifecycle_execution)

    def test_shutdown(self):
      self.ansible_processor.shutdown()

      request_id = uuid.uuid4().hex

      self.ansible_processor.run_lifecycle({
        'lifecycle_name': 'install',
        'lifecycle_path': DirectoryTree('./'),
        'system_properties': {
        },
        'properties': {
        },
        'deployment_location': {
        },
        'request_id': request_id
      })

      self.check_response(LifecycleExecution(request_id, STATUS_FAILED, FailureDetails(FAILURE_CODE_INSUFFICIENT_CAPACITY, "Driver is inactive"), {}))

    def test_max_queue_size(self):
        request_id1 = uuid.uuid4().hex
        request_id2 = uuid.uuid4().hex

        self.mock_ansible_client.run_lifecycle_playbook.side_effect = [
            LifecycleExecution(request_id1, STATUS_COMPLETE, None, {
              'prop1': 'output__value1'
            }),
            # simulate a long-running task
            sleep(request_id2)
          ]

        # with a queue size of 1
        property_groups = PropertyGroups()
        property_groups.add_property_group(AnsibleProperties())
        process_properties = ProcessProperties()
        setattr(process_properties, 'max_queue_size', 1)
        property_groups.add_property_group(process_properties)
        property_groups.add_property_group(CacheProperties())
        configuration = BootstrapApplicationConfiguration(app_name='test', property_sources=[], property_groups=property_groups, service_configurators=[], api_configurators=[], api_error_converter=None)
        self.ansible_processor = AnsibleProcessorService(configuration, self.request_queue, self.mock_ansible_client, messaging_service=self.mock_messaging_service)

        self.ansible_processor.run_lifecycle({
          'lifecycle_name': 'install',
          'lifecycle_path': DirectoryTree('./'),
          'system_properties': {
          },
          'properties': {
          },
          'deployment_location': {
          },
          'request_id': request_id1
        })

        self.ansible_processor.run_lifecycle({
          'lifecycle_name': 'install',
          'lifecycle_path': DirectoryTree('./'),
          'system_properties': {
          },
          'properties': {
          },
          'deployment_location': {
          },
          'request_id': request_id2
        })

        self.check_response(LifecycleExecution(request_id2, STATUS_FAILED, FailureDetails(FAILURE_CODE_INSUFFICIENT_CAPACITY, "Request cannot be handled, driver is overloaded"), {}))