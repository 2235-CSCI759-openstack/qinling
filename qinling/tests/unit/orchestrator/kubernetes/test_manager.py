# Copyright 2018 AWCloud Software Co., Ltd.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import mock
import yaml

from oslo_config import cfg

from qinling import config
from qinling import exceptions as exc
from qinling.orchestrator.kubernetes import manager as k8s_manager
from qinling.tests.unit import base
from qinling.utils import common

CONF = cfg.CONF
SERVICE_PORT = 9090
SERVICE_ADDRESS_EXTERNAL = '1.2.3.4'


class TestKubernetesManager(base.BaseTest):
    def setUp(self):
        super(TestKubernetesManager, self).setUp()
        CONF.register_opts(config.kubernetes_opts, config.KUBERNETES_GROUP)
        self.conf = CONF
        self.qinling_endpoint = 'http://127.0.0.1:7070'
        self.k8s_v1_api = mock.Mock()
        self.k8s_v1_ext = mock.Mock()
        clients = {'v1': self.k8s_v1_api,
                   'v1extention': self.k8s_v1_ext}
        mock.patch(
            'qinling.orchestrator.kubernetes.utils.get_k8s_clients',
            return_value=clients
        ).start()
        self.fake_namespace = self.rand_name('namespace',
                                             prefix='TestKubernetesManager')
        self.override_config('namespace', self.fake_namespace,
                             config.KUBERNETES_GROUP)
        namespace = mock.Mock()
        namespace.metadata.name = self.fake_namespace
        namespaces = mock.Mock()
        namespaces.items = [namespace]
        self.k8s_v1_api.list_namespace.return_value = namespaces
        self.manager = k8s_manager.KubernetesManager(
            self.conf, self.qinling_endpoint)

    def _create_service(self):
        port = mock.Mock()
        port.node_port = SERVICE_PORT
        service = mock.Mock()
        service.spec.ports = [port]
        return service

    def _create_nodes_with_external_ip(self):
        addr1 = mock.Mock()
        addr1.type = 'UNKNOWN TYPE'
        addr2 = mock.Mock()
        addr2.type = 'ExternalIP'
        addr2.address = SERVICE_ADDRESS_EXTERNAL
        item = mock.Mock()
        item.status.addresses = [addr1, addr2]
        nodes = mock.Mock()
        nodes.items = [item]
        return nodes

    def test_create_pool(self):
        ret = mock.Mock()
        ret.status.replicas = 5
        ret.status.available_replicas = 5
        self.k8s_v1_ext.read_namespaced_deployment.return_value = ret
        fake_replicas = 5
        self.override_config('replicas', fake_replicas,
                             config.KUBERNETES_GROUP)
        fake_deployment_name = self.rand_name('deployment',
                                              prefix='TestKubernetesManager')
        fake_image = self.rand_name('image', prefix='TestKubernetesManager')

        self.manager.create_pool(fake_deployment_name, fake_image)

        deployment_body = self.manager.deployment_template.render(
            {
                'name': fake_deployment_name,
                'labels': {'runtime_id': fake_deployment_name},
                'replicas': fake_replicas,
                'container_name': 'worker',
                'image': fake_image
            }
        )
        self.k8s_v1_ext.create_namespaced_deployment.assert_called_once_with(
            body=yaml.safe_load(deployment_body),
            namespace=self.fake_namespace)
        self.k8s_v1_ext.read_namespaced_deployment.assert_called_once_with(
            fake_deployment_name, self.fake_namespace)

    def test_delete_pool(self):
        # Deleting namespaced service is also tested in this.
        svc1 = mock.Mock()
        svc1_name = self.rand_name('service', prefix='TestKubernetesManager')
        svc1.metadata.name = svc1_name
        svc2 = mock.Mock()
        svc2_name = self.rand_name('service', prefix='TestKubernetesManager')
        svc2.metadata.name = svc2_name
        services = mock.Mock()
        services.items = [svc1, svc2]
        self.k8s_v1_api.list_namespaced_service.return_value = services
        fake_deployment_name = self.rand_name('deployment',
                                              prefix='TestKubernetesManager')

        self.manager.delete_pool(fake_deployment_name)

        del_rep = self.k8s_v1_ext.delete_collection_namespaced_replica_set
        del_rep.assert_called_once_with(
            self.fake_namespace,
            label_selector='runtime_id=%s' % fake_deployment_name)
        delete_service_calls = [
            mock.call(svc1_name, self.fake_namespace),
            mock.call(svc2_name, self.fake_namespace),
        ]
        self.k8s_v1_api.delete_namespaced_service.assert_has_calls(
            delete_service_calls)
        self.assertEqual(
            2, self.k8s_v1_api.delete_namespaced_service.call_count)
        del_dep = self.k8s_v1_ext.delete_collection_namespaced_deployment
        del_dep.assert_called_once_with(
            self.fake_namespace,
            label_selector='runtime_id=%s' % fake_deployment_name,
            field_selector='metadata.name=%s' % fake_deployment_name)
        del_pod = self.k8s_v1_api.delete_collection_namespaced_pod
        del_pod.assert_called_once_with(
            self.fake_namespace,
            label_selector='runtime_id=%s' % fake_deployment_name)

    def test_update_pool(self):
        fake_deployment_name = self.rand_name('deployment',
                                              prefix='TestKubernetesManager')
        image = self.rand_name('image', prefix='TestKubernetesManager')
        body = {
            'spec': {
                'template': {
                    'spec': {
                        'containers': [
                            {
                                'name': 'worker',
                                'image': image
                            }
                        ]
                    }
                }
            }
        }
        ret = mock.Mock()
        ret.status.unavailable_replicas = 0
        self.k8s_v1_ext.read_namespaced_deployment_status.return_value = ret

        update_result = self.manager.update_pool(fake_deployment_name,
                                                 image=image)

        self.assertTrue(update_result)
        self.k8s_v1_ext.patch_namespaced_deployment.assert_called_once_with(
            fake_deployment_name, self.fake_namespace, body)
        read_status = self.k8s_v1_ext.read_namespaced_deployment_status
        read_status.assert_called_once_with(fake_deployment_name,
                                            self.fake_namespace)

    def test_update_pool_retry(self):
        fake_deployment_name = self.rand_name('deployment',
                                              prefix='TestKubernetesManager')
        image = self.rand_name('image', prefix='TestKubernetesManager')
        ret1 = mock.Mock()
        ret1.status.unavailable_replicas = 1
        ret2 = mock.Mock()
        ret2.status.unavailable_replicas = 0
        self.k8s_v1_ext.read_namespaced_deployment_status.side_effect = [
            ret1, ret2]

        update_result = self.manager.update_pool(fake_deployment_name,
                                                 image=image)

        self.assertTrue(update_result)
        self.k8s_v1_ext.patch_namespaced_deployment.assert_called_once_with(
            fake_deployment_name, self.fake_namespace, mock.ANY)
        read_status = self.k8s_v1_ext.read_namespaced_deployment_status
        self.assertEqual(2, read_status.call_count)

    def test_update_pool_rollback(self):
        fake_deployment_name = self.rand_name('deployment',
                                              prefix='TestKubernetesManager')
        image = self.rand_name('image', prefix='TestKubernetesManager')
        ret = mock.Mock()
        ret.status.unavailable_replicas = 1
        self.k8s_v1_ext.read_namespaced_deployment_status.return_value = ret
        rollback_body = {
            "name": fake_deployment_name,
            "rollbackTo": {
                "revision": 0
            }
        }

        update_result = self.manager.update_pool(fake_deployment_name,
                                                 image=image)

        self.assertFalse(update_result)
        self.k8s_v1_ext.patch_namespaced_deployment.assert_called_once_with(
            fake_deployment_name, self.fake_namespace, mock.ANY)
        read_status = self.k8s_v1_ext.read_namespaced_deployment_status
        self.assertEqual(5, read_status.call_count)
        rollback = self.k8s_v1_ext.create_namespaced_deployment_rollback
        rollback.assert_called_once_with(
            fake_deployment_name, self.fake_namespace, rollback_body)

    def test_prepare_execution(self):
        pod = mock.Mock()
        pod.metadata.name = self.rand_name('pod',
                                           prefix='TestKubernetesManager')
        pod.metadata.labels = {'pod1_key1': 'pod1_value1'}
        list_pod_ret = mock.Mock()
        list_pod_ret.items = [pod]
        self.k8s_v1_api.list_namespaced_pod.return_value = list_pod_ret
        self.k8s_v1_api.create_namespaced_service.return_value = (
            self._create_service()
        )
        self.k8s_v1_api.list_node.return_value = (
            self._create_nodes_with_external_ip()
        )
        runtime_id = common.generate_unicode_uuid()
        function_id = common.generate_unicode_uuid()

        pod_names, service_url = self.manager.prepare_execution(
            function_id, image=None, identifier=runtime_id,
            labels={'runtime_id': runtime_id})

        self.assertEqual(pod.metadata.name, pod_names)
        self.assertEqual(
            'http://%s:%s' % (SERVICE_ADDRESS_EXTERNAL, SERVICE_PORT),
            service_url)

        # in _choose_available_pods
        self.k8s_v1_api.list_namespaced_pod.assert_called_once_with(
            self.fake_namespace,
            label_selector='function_id=%s' % function_id)

        # in _prepare_pod -> _update_pod_label
        pod_labels = {'pod1_key1': 'pod1_value1', 'function_id': function_id}
        body = {'metadata': {'labels': pod_labels}}
        self.k8s_v1_api.patch_namespaced_pod.assert_called_once_with(
            pod.metadata.name, self.fake_namespace, body)

        # in _prepare_pod
        service_body = self.manager.service_template.render(
            {
                'service_name': 'service-%s' % function_id,
                'labels': {'function_id': function_id,
                           'runtime_id': runtime_id},
                'selector': pod_labels
            }
        )
        self.k8s_v1_api.create_namespaced_service.assert_called_once_with(
            self.fake_namespace, yaml.safe_load(service_body))

    def test_prepare_execution_with_image(self):
        function_id = common.generate_unicode_uuid()
        image = self.rand_name('image', prefix='TestKubernetesManager')
        identifier = ('%s-%s' % (
                      common.generate_unicode_uuid(dashed=False),
                      function_id)
                      )[:63]

        pod_name, url = self.manager.prepare_execution(
            function_id, image=image, identifier=identifier)

        self.assertEqual(identifier, pod_name)
        self.assertIsNone(url)

        # in _create_pod
        pod_body = self.manager.pod_template.render(
            {
                'pod_name': identifier,
                'labels': {'function_id': function_id},
                'pod_image': image,
                'input': []
            }
        )
        self.k8s_v1_api.create_namespaced_pod.assert_called_once_with(
            self.fake_namespace, body=yaml.safe_load(pod_body))

    def test_prepare_execution_no_worker_available(self):
        ret_pods = mock.Mock()
        ret_pods.items = []
        self.k8s_v1_api.list_namespaced_pod.return_value = ret_pods
        function_id = common.generate_unicode_uuid()
        runtime_id = common.generate_unicode_uuid()
        labels = {'runtime_id': runtime_id}

        self.assertRaisesRegex(
            exc.OrchestratorException,
            "^Execution preparation failed\.$",
            self.manager.prepare_execution,
            function_id, image=None, identifier=runtime_id, labels=labels)

        # in _choose_available_pods
        list_calls = [
            mock.call(self.fake_namespace,
                      label_selector='function_id=%s' % function_id),
            mock.call(self.fake_namespace,
                      label_selector='!function_id,runtime_id=%s' % runtime_id)
        ]
        self.k8s_v1_api.list_namespaced_pod.assert_has_calls(list_calls)
        self.assertEqual(2, self.k8s_v1_api.list_namespaced_pod.call_count)

    def test_prepare_execution_pod_preparation_failed(self):
        pod = mock.Mock()
        pod.metadata.name = self.rand_name('pod',
                                           prefix='TestKubernetesManager')
        pod.metadata.labels = None
        ret_pods = mock.Mock()
        ret_pods.items = [pod]
        self.k8s_v1_api.list_namespaced_pod.return_value = ret_pods
        exception = RuntimeError()
        exception.status = 500
        self.k8s_v1_api.create_namespaced_service.side_effect = exception
        function_id = common.generate_unicode_uuid()
        runtime_id = common.generate_unicode_uuid()

        with mock.patch.object(
            self.manager, 'delete_function'
        ) as delete_function_mock:
            self.assertRaisesRegex(
                exc.OrchestratorException,
                '^Execution preparation failed\.$',
                self.manager.prepare_execution,
                function_id, image=None, identifier=runtime_id,
                labels={'runtime_id': runtime_id})

            delete_function_mock.assert_called_once_with(
                function_id,
                {'runtime_id': runtime_id, 'function_id': function_id})

    def test_run_execution(self):
        pod = mock.Mock()
        pod.status.phase = 'Succeeded'
        self.k8s_v1_api.read_namespaced_pod.return_value = pod
        fake_output = 'fake output'
        self.k8s_v1_api.read_namespaced_pod_log.return_value = fake_output
        execution_id = common.generate_unicode_uuid()
        function_id = common.generate_unicode_uuid()

        result, output = self.manager.run_execution(execution_id, function_id)

        self.k8s_v1_api.read_namespaced_pod.assert_called_once_with(
            None, self.fake_namespace)
        self.k8s_v1_api.read_namespaced_pod_log.assert_called_once_with(
            None, self.fake_namespace)
        self.assertTrue(result)
        self.assertEqual(fake_output, output)

    @mock.patch('qinling.engine.utils.get_request_data')
    @mock.patch('qinling.engine.utils.url_request')
    def test_run_execution_with_service_url(self, url_request_mock,
                                            get_request_data_mock):
        fake_output = 'fake output'
        url_request_mock.return_value = (True, 'fake output')
        fake_data = 'some data'
        get_request_data_mock.return_value = fake_data
        execution_id = common.generate_unicode_uuid()
        function_id = common.generate_unicode_uuid()

        result, output = self.manager.run_execution(
            execution_id, function_id, service_url='FAKE_URL')

        get_request_data_mock.assert_called_once_with(
            self.conf, function_id, execution_id, None, 'main.main', None,
            self.qinling_endpoint)
        url_request_mock.assert_called_once_with(
            self.manager.session, 'FAKE_URL/execute', body=fake_data)
        self.assertTrue(result)
        self.assertEqual(fake_output, output)

    def test_run_execution_retry(self):
        pod1 = mock.Mock()
        pod1.status.phase = ''
        pod2 = mock.Mock()
        pod2.status.phase = 'Succeeded'
        self.k8s_v1_api.read_namespaced_pod.side_effect = [pod1, pod2]
        fake_output = 'fake output'
        self.k8s_v1_api.read_namespaced_pod_log.return_value = fake_output
        execution_id = common.generate_unicode_uuid()
        function_id = common.generate_unicode_uuid()

        result, output = self.manager.run_execution(execution_id, function_id)

        self.assertEqual(2, self.k8s_v1_api.read_namespaced_pod.call_count)
        self.k8s_v1_api.read_namespaced_pod_log.assert_called_once_with(
            None, self.fake_namespace)
        self.assertTrue(result)
        self.assertEqual(fake_output, output)

    def test_run_execution_failed(self):
        self.k8s_v1_api.read_namespaced_pod.side_effect = RuntimeError
        execution_id = common.generate_unicode_uuid()
        function_id = common.generate_unicode_uuid()

        result, output = self.manager.run_execution(execution_id, function_id)

        self.k8s_v1_api.read_namespaced_pod.assert_called_once_with(
            None, self.fake_namespace)
        self.k8s_v1_api.read_namespaced_pod_log.assert_not_called()
        self.assertFalse(result)
        self.assertEqual({'error': 'Function execution failed.'}, output)

    def test_delete_function(self):
        # Deleting namespaced service is also tested in this.
        svc1 = mock.Mock()
        svc1_name = self.rand_name('service', prefix='TestKubernetesManager')
        svc1.metadata.name = svc1_name
        svc2 = mock.Mock()
        svc2_name = self.rand_name('service', prefix='TestKubernetesManager')
        svc2.metadata.name = svc2_name
        services = mock.Mock()
        services.items = [svc1, svc2]
        self.k8s_v1_api.list_namespaced_service.return_value = services
        function_id = common.generate_unicode_uuid()

        self.manager.delete_function(function_id)

        self.k8s_v1_api.list_namespaced_service.assert_called_once_with(
            self.fake_namespace, label_selector='function_id=%s' % function_id)
        delete_service_calls = [
            mock.call(svc1_name, self.fake_namespace),
            mock.call(svc2_name, self.fake_namespace)
        ]
        self.k8s_v1_api.delete_namespaced_service.assert_has_calls(
            delete_service_calls)
        self.assertEqual(
            2, self.k8s_v1_api.delete_namespaced_service.call_count)
        delete_pod = self.k8s_v1_api.delete_collection_namespaced_pod
        delete_pod.assert_called_once_with(
            self.fake_namespace, label_selector='function_id=%s' % function_id)

    def test_delete_function_with_labels(self):
        services = mock.Mock()
        services.items = []
        labels = {'key1': 'value1', 'key2': 'value2'}
        selector = common.convert_dict_to_string(labels)
        self.k8s_v1_api.list_namespaced_service.return_value = services
        function_id = common.generate_unicode_uuid()

        self.manager.delete_function(function_id, labels=labels)

        self.k8s_v1_api.list_namespaced_service.assert_called_once_with(
            self.fake_namespace, label_selector=selector)
        self.k8s_v1_api.delete_namespaced_service.assert_not_called()
        delete_pod = self.k8s_v1_api.delete_collection_namespaced_pod
        delete_pod.assert_called_once_with(
            self.fake_namespace, label_selector=selector)

    def test_scaleup_function(self):
        pod = mock.Mock()
        pod.metadata.name = self.rand_name('pod',
                                           prefix='TestKubernetesManager')
        pod.metadata.labels = {'pod1_key1': 'pod1_value1'}
        list_pod_ret = mock.Mock()
        list_pod_ret.items = [pod]
        self.k8s_v1_api.list_namespaced_pod.return_value = list_pod_ret
        self.k8s_v1_api.create_namespaced_service.return_value = (
            self._create_service()
        )
        self.k8s_v1_api.list_node.return_value = (
            self._create_nodes_with_external_ip()
        )
        runtime_id = common.generate_unicode_uuid()
        function_id = common.generate_unicode_uuid()

        pod_names, service_url = self.manager.scaleup_function(
            function_id, identifier=runtime_id)

        self.assertEqual([pod.metadata.name], pod_names)
        self.assertEqual(
            'http://%s:%s' % (SERVICE_ADDRESS_EXTERNAL, SERVICE_PORT),
            service_url)

        # in _choose_available_pods
        self.k8s_v1_api.list_namespaced_pod.assert_called_once_with(
            self.fake_namespace,
            label_selector='!function_id,runtime_id=%s' % runtime_id)

        # in _prepare_pod -> _update_pod_label
        pod_labels = {'pod1_key1': 'pod1_value1', 'function_id': function_id}
        body = {'metadata': {'labels': pod_labels}}
        self.k8s_v1_api.patch_namespaced_pod.assert_called_once_with(
            pod.metadata.name, self.fake_namespace, body)

        # in _prepare_pod
        service_body = self.manager.service_template.render(
            {
                'service_name': 'service-%s' % function_id,
                'labels': {'function_id': function_id,
                           'runtime_id': runtime_id},
                'selector': pod_labels
            }
        )
        self.k8s_v1_api.create_namespaced_service.assert_called_once_with(
            self.fake_namespace, yaml.safe_load(service_body))

    def test_scaleup_function_not_enough_workers(self):
        runtime_id = common.generate_unicode_uuid()
        function_id = common.generate_unicode_uuid()
        ret_pods = mock.Mock()
        ret_pods.items = [mock.Mock()]
        self.k8s_v1_api.list_namespaced_pod.return_value = ret_pods

        self.assertRaisesRegex(
            exc.OrchestratorException,
            "^Not enough workers available\.$",
            self.manager.scaleup_function,
            function_id, identifier=runtime_id, count=2)

    def test_delete_worker(self):
        pod_name = self.rand_name('pod', prefix='TestKubernetesManager')

        self.manager.delete_worker(pod_name)

        self.k8s_v1_api.delete_namespaced_pod.assert_called_once_with(
            pod_name, self.fake_namespace, {})