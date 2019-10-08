import json
import logging
import time
import os
import sys, traceback
import threading
import signal
from multiprocessing import Process, RawValue, Lock, Pipe
from multiprocessing.pool import Pool
from collections import namedtuple
from ignition.model.lifecycle import LifecycleExecution, STATUS_COMPLETE, STATUS_FAILED, STATUS_IN_PROGRESS
from ignition.model.failure import FailureDetails, FAILURE_CODE_INFRASTRUCTURE_ERROR, FAILURE_CODE_INTERNAL_ERROR, FAILURE_CODE_RESOURCE_NOT_FOUND, FAILURE_CODE_INSUFFICIENT_CAPACITY
from ignition.service.lifecycle import LifecycleDriverCapability
from ignition.service.framework import Service, Capability, interface
from ansibledriver.service.queue import SHUTDOWN_MESSAGE
from ignition.service.config import ConfigurationPropertiesGroup

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class AnsibleProcessorCapability(Capability):

    @interface
    def queue_status(self):
        pass

class ProcessProperties(ConfigurationPropertiesGroup):
    def __init__(self):
        super().__init__('process')
        # apply defaults (correct settings will be picked up from config file or environment variables)
        self.process_pool_size = 10
        self.max_concurrent_ansible_processes = 10
        self.max_queue_size = 100
        self.use_pool = False
        self.is_threaded = False

class AnsibleProcessorService(Service, AnsibleProcessorCapability):
    def __init__(self, configuration, request_queue, ansible_client, **kwargs):
        if 'messaging_service' not in kwargs:
            raise ValueError('messaging_service argument not provided')
        self.messaging_service = kwargs.get('messaging_service')

        self.process_properties = configuration.property_groups.get_property_group(ProcessProperties)

        # lifecycle requests are placed on this queue
        self.request_queue = request_queue

        self.ansible_client = ansible_client
        self.counter = Counter()

        # a pipe used for communication with Ansible worker processes
        self.recv_pipe, self.send_pipe = Pipe(False)

        # acknowledge but ignore child process exits (to prevent zombie child processes)
        self.sigchld_handler = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        signal.signal(signal.SIGINT, self.sigint_handler)

        if self.process_properties.use_pool:
          # a pool of (Ansible) processes reads from the request_queue
          # we don't using a multiprocessing.Pool here because it uses daemon processes which cannot
          # create sub-processes (and Ansible requires this)
          self.pool = [None] * self.process_properties.process_pool_size
          for i in range(self.process_properties.process_pool_size):
            self.pool[i] = Process(target=self.ansible_process_worker, args=(self.request_queue, self.send_pipe, ))
            # self.pool[i].daemon = True
            self.pool[i].start()
        else:
          self.queue_thread = QueueThread(self, self.ansible_client, self.send_pipe, self.process_properties, self.request_queue, self.counter)

        # Ansible process reponse thread listens for messages on the recv_pipe and updates
        # the set of responses
        self.responses_thread = ResponsesThread(self, self.recv_pipe)

        self.active = True

        self.responses_thread.start()
        if self.queue_thread is not None:
          self.queue_thread.start()

    def run_lifecycle(self, request):
      if 'request_id' not in request:
        raise ValueError('Request must have a request_id')
      if 'lifecycle_name' not in request:
        raise ValueError('Request must have a lifecycle_name')
      if 'lifecycle_path' not in request:
        raise ValueError('Request must have a lifecycle_path')

      logger.debug('request_queue.size {0} max_queue_size {1}'.format(self.request_queue.size(), self.process_properties.max_queue_size))
      if self.active == True:
        if self.request_queue.size() >= self.process_properties.max_queue_size:
          self.messaging_service.send_lifecycle_execution(LifecycleExecution(request['request_id'], STATUS_FAILED, FailureDetails(FAILURE_CODE_INSUFFICIENT_CAPACITY, "Request cannot be handled, driver is overloaded"), {}))
        else:
          self.request_queue.put(request)
      else:
        # inactive, just return a standard response
        self.messaging_service.send_lifecycle_execution(LifecycleExecution(request['request_id'], STATUS_FAILED, FailureDetails(FAILURE_CODE_INSUFFICIENT_CAPACITY, "Driver is inactive"), {}))

    def queue_status(self):
      return self.request_queue.queue_status()

    def ansible_process_worker(self, request_queue, send_pipe):
      logger.info('ansible_queue_worker init')
      # make sure Ansible processes are acknowledged to avoid zombie processes
      signal.signal(signal.SIGCHLD, signal.SIG_IGN)
      while(True):
        try:
          request = request_queue.next()
          if request is not None:
            send_pipe.send(self.ansible_client.run_lifecycle_playbook(request))

            logger.info('Ansible worker finished for request {0}'.format(request))
        except Exception as e:
          logger.error('Unexpected exception {0}'.format(e))
          traceback.print_exc(file=sys.stderr)
          # don't want the worker to die without knowing the cause, so catch all exceptions
          if request is not None:
            send_pipe.send(LifecycleExecution(request['request_id'], STATUS_FAILED, FailureDetails(FAILURE_CODE_INTERNAL_ERROR, "Unexpected exception: {0}".format(e)), {}))

    def sigint_handler(self, sig, frame):
      logger.debug('sigint_handler')
      self.shutdown()
      exit(0)

    def shutdown(self):
      logger.info('shutdown')

      self.active = False

      self.request_queue.shutdown()
      self.send_pipe.close()

    def to_lifecycle_execution(self, json):
      if json.get('failure_details', None) is not None:
        failure_details = FailureDetails(json['failure_details']['failure_code'], json['failure_details']['description'])
      else:
        failure_details = None
      return LifecycleExecution(json['request_id'], json['status'], failure_details, json['outputs'])

class QueueThread(threading.Thread):

    def __init__(self, ansible_processor, ansible_client, send_pipe, process_properties, request_queue, counter):
      self.ansible_processor = ansible_processor
      self.ansible_client = ansible_client
      self.send_pipe = send_pipe
      self.process_properties = process_properties
      self.request_queue = request_queue
      self.counter = counter
      super().__init__(daemon = True)

    def run(self):
      while self.ansible_processor.active:
        try:
          request = self.request_queue.next()
          if request is not None:
            if request == SHUTDOWN_MESSAGE:
              self.request_queue.task_done()
              break
            elif self.counter.value() < self.process_properties.max_concurrent_ansible_processes:
              try:
                logger.info('Got request from queue: {0}'.format(request))
                if(request == SHUTDOWN_MESSAGE):
                  self.request_queue.task_done()
                  break
                else:
                  self.counter.increment()

                  if self.process_properties.is_threaded:
                    worker = AnsibleWorkerThread(self.ansible_client, request, self.send_pipe)
                    worker.start()
                    logger.info('Request processing started for request {0} with thread {1}'.format(request, worker.ident))
                  else:
                    worker = AnsibleWorkerProcess(self.ansible_processor.sigchld_handler, self.ansible_client, request, self.send_pipe)
                    worker.start()
                    logger.info('Request processing started for request {0} with pid {1}'.format(request, worker.pid))
              finally:
                self.request_queue.task_done()
            else:
              self.request_queue.task_done()
              logger.debug('Max processes reached, re-queuing request {0}'.format(request))
              # this may increase the queue above the requested bounds but the increase will be bounded
              # by the max processes setting and not by the number of requests coming in
              # TODO will this put to back of queue?
              self.request_queue.put(request)
        except Exception as e:
          traceback.print_exc(file=sys.stdout)
          logger.error('Unexpected exception {0}'.format(e))

class AnsibleWorkerThread(threading.Thread):

    def __init__(self, ansible_client, request, send_pipe):
      self.ansible_client = ansible_client
      self.request = request
      self.send_pipe = send_pipe
      super().__init__(daemon = True)

    def run(self):
      try:
        if self.request is not None:
          resp = self.ansible_client.run_lifecycle_playbook(self.request)
          if resp is not None:
            logger.info('Ansible worker finished for request {0} response {1}'.format(self.request, resp))
            self.send_pipe.send(resp)
          else:
            logger.warn("Empty response from Ansible worker for request {0}".format(self.request))
        else:
          pass
          # TODO
      except Exception as e:
        logger.error('Unexpected exception {0}'.format(e))
        traceback.print_exc(file=sys.stderr)
        # don't want the worker to die without knowing the cause, so catch all exceptions
        if self.request is not None:
          self.send_pipe.send(LifecycleExecution(self.request['request_id'], STATUS_FAILED, FailureDetails(FAILURE_CODE_INTERNAL_ERROR, "Unexpected exception: {0}".format(e)), {}))

class AnsibleWorkerProcess(Process):

    def __init__(self, sigchld_handler, ansible_client, request, send_pipe):
      self.ansible_client = ansible_client
      self.request = request
      self.send_pipe = send_pipe
      self.sigchld_handler = sigchld_handler
      # need to reset SIGCHLD handler (setting is inherited from parent process) so that Ansible can override it
      # signal.signal(signal.SIGCHLD, sigchld_handler)
      # # we want to handle child process termination
      # self.daemon = False
      super().__init__(daemon = False)

    def run(self):
      # need to reset SIGCHLD handler (setting is inherited from parent process) so that Ansible can override it
      signal.signal(signal.SIGCHLD, self.sigchld_handler)

      try:
        if self.request is not None:
          resp = self.ansible_client.run_lifecycle_playbook(self.request)
          if resp is not None:
            logger.info('Ansible worker finished for request {0} response {1}'.format(self.request, resp))
            self.send_pipe.send(resp)
          else:
            logger.warn("Empty response from Ansible worker for request {0}".format(self.request))
        else:
          pass
          # TODO
      except Exception as e:
        logger.error('Unexpected exception {0}'.format(e))
        traceback.print_exc(file=sys.stderr)
        # don't want the worker to die without knowing the cause, so catch all exceptions
        if self.request is not None:
          self.send_pipe.send(LifecycleExecution(self.request['request_id'], STATUS_FAILED, FailureDetails(FAILURE_CODE_INTERNAL_ERROR, "Unexpected exception: {0}".format(e)), {}))

class ResponsesThread(threading.Thread):

    def __init__(self, ansible_processor_service, recv_pipe):
      self.ansible_processor_service = ansible_processor_service
      self.recv_pipe = recv_pipe
      super().__init__(daemon = True)

    def run(self):
      while self.ansible_processor_service.active:
        try:
          result = self.recv_pipe.recv()
          if result is not None:
            logger.info('responses thread received {0}'.format(result))
            self.ansible_processor_service.messaging_service.send_lifecycle_execution(result)
          else:
            # nothing to do
            pass
        except EOFError as error:
          # nothing to do - ignore
          pass

class Counter(object):
    def __init__(self, value=0):
        # RawValue because we don't need it to create a Lock:
        self.val = RawValue('i', value)
        self.lock = Lock()

    def increment(self):
        with self.lock:
            self.val.value += 1

    def decrement(self):
        with self.lock:
            self.val.value -= 1

    def value(self):
        with self.lock:
            return self.val.value