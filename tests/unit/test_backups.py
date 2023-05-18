# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
import unittest
from unittest.mock import PropertyMock, patch

from ops import ActiveStatus, BlockedStatus
from ops.testing import Harness

from charm import PostgresqlOperatorCharm
from constants import PEER

ANOTHER_CLUSTER_REPOSITORY_ERROR_MESSAGE = "the S3 repository has backups from another cluster"
FAILED_TO_ACCESS_CREATE_BUCKET_ERROR_MESSAGE = (
    "failed to access/create the bucket, check your S3 settings"
)
FAILED_TO_INITIALIZE_STANZA_ERROR_MESSAGE = "failed to initialize stanza, check your S3 settings"
S3_PARAMETERS_RELATION = "s3-parameters"


class TestPostgreSQLBackups(unittest.TestCase):
    @patch("charm.KubernetesServicePatch", lambda x, y: None)
    def setUp(self):
        self.harness = Harness(PostgresqlOperatorCharm)
        self.addCleanup(self.harness.cleanup)

        # Set up the initial relation and hooks.
        self.peer_rel_id = self.harness.add_relation(PEER, "postgresql-k8s")
        self.harness.add_relation_unit(self.peer_rel_id, "postgresql-k8s/0")
        self.harness.begin()
        self.charm = self.harness.charm

    def relate_to_s3_integrator(self):
        self.s3_rel_id = self.harness.add_relation(S3_PARAMETERS_RELATION, "s3-integrator")

    def remove_relation_from_s3_integrator(self):
        self.harness.remove_relation(S3_PARAMETERS_RELATION, "s3-integrator")
        self.s3_rel_id = None

    def test_stanza_name(self):
        self.assertEqual(
            self.charm.backup.stanza_name, f"{self.charm.model.name}.{self.charm.cluster_name}"
        )

    def test_are_backup_settings_ok(self):
        # Test without S3 relation.
        self.assertEqual(
            self.charm.backup._are_backup_settings_ok(),
            (False, "Relation with s3-integrator charm missing, cannot create/restore backup."),
        )

        # Test when there are missing S3 parameters.
        self.relate_to_s3_integrator()
        self.assertEqual(
            self.charm.backup._are_backup_settings_ok(),
            (False, "Missing S3 parameters: ['bucket', 'access-key', 'secret-key']"),
        )

        # Test when all required parameters are provided.
        with patch("charm.PostgreSQLBackups._retrieve_s3_parameters") as _retrieve_s3_parameters:
            _retrieve_s3_parameters.return_value = ["bucket", "access-key", "secret-key"], []
            self.assertEqual(
                self.charm.backup._are_backup_settings_ok(),
                (True, None),
            )

    @patch("charm.PostgreSQLBackups._are_backup_settings_ok")
    @patch("charm.Patroni.member_started", new_callable=PropertyMock)
    @patch("ops.model.Application.planned_units")
    @patch("charm.PostgresqlOperatorCharm.is_primary", new_callable=PropertyMock)
    def test_can_unit_perform_backup(
        self, _is_primary, _planned_units, _member_started, _are_backup_settings_ok
    ):
        # Test when the unit is in a blocked state.
        self.charm.unit.status = BlockedStatus("fake blocked state")
        self.assertEqual(
            self.charm.backup._can_unit_perform_backup(),
            (False, "Unit is in a blocking state"),
        )

        # Test when running the check in the primary, there are replicas and TLS is enabled.
        self.charm.unit.status = ActiveStatus()
        _is_primary.return_value = True
        _planned_units.return_value = 2
        with self.harness.hooks_disabled():
            self.harness.update_relation_data(
                self.peer_rel_id,
                self.charm.unit.name,
                {"tls": "True"},
            )
        self.assertEqual(
            self.charm.backup._can_unit_perform_backup(),
            (False, "Unit cannot perform backups as it is the cluster primary"),
        )

        # Test when running the check in a replica and TLS is disabled.
        _is_primary.return_value = False
        with self.harness.hooks_disabled():
            self.harness.update_relation_data(
                self.peer_rel_id,
                self.charm.unit.name,
                {"tls": ""},
            )
        self.assertEqual(
            self.charm.backup._can_unit_perform_backup(),
            (False, "Unit cannot perform backups as TLS is not enabled"),
        )

        # Test when Patroni or PostgreSQL hasn't started yet.
        _is_primary.return_value = True
        _member_started.return_value = False
        self.assertEqual(
            self.charm.backup._can_unit_perform_backup(),
            (False, "Unit cannot perform backups as it's not in running state"),
        )

        # Test when the stanza was not initialised yet.
        _member_started.return_value = True
        self.assertEqual(
            self.charm.backup._can_unit_perform_backup(),
            (False, "Stanza was not initialised"),
        )

        # Test when S3 parameters are not ok.
        with self.harness.hooks_disabled():
            self.harness.update_relation_data(
                self.peer_rel_id,
                self.charm.app.name,
                {"stanza": self.charm.backup.stanza_name},
            )
        _are_backup_settings_ok.return_value = (False, "fake error message")
        self.assertEqual(
            self.charm.backup._can_unit_perform_backup(),
            (False, "fake error message"),
        )

        # Test when everything is ok to run a backup.
        _are_backup_settings_ok.return_value = (True, None)
        self.assertEqual(
            self.charm.backup._can_unit_perform_backup(),
            (True, None),
        )

    @patch("charm.Patroni.reload_patroni_configuration")
    @patch("charm.Patroni.member_started", new_callable=PropertyMock)
    @patch("charm.PostgresqlOperatorCharm.update_config")
    @patch("charm.PostgreSQLBackups._execute_command")
    def test_can_use_s3_repository(
        self, _execute_command, _update_config, _member_started, _reload_patroni_configuration
    ):
        # Define the stanza name inside the unit relation data.
        with self.harness.hooks_disabled():
            self.harness.update_relation_data(
                self.peer_rel_id,
                self.charm.app.name,
                {"stanza": self.charm.backup.stanza_name},
            )

        # Test when nothing is returned from the pgBackRest info command.
        _execute_command.return_value = (None, None)
        self.assertEqual(
            self.charm.backup.can_use_s3_repository(),
            (False, FAILED_TO_INITIALIZE_STANZA_ERROR_MESSAGE),
        )

        # Test when the unit is a replica and there is a backup from another cluster
        # in the S3 repository.
        _execute_command.return_value = (
            f'[{{"name": "another-model.{self.charm.cluster_name}"}}]',
            None,
        )
        self.assertEqual(
            self.charm.backup.can_use_s3_repository(),
            (True, None),
        )

        # Assert that the stanza name is still in the unit relation data.
        self.assertEqual(
            self.harness.get_relation_data(self.peer_rel_id, self.charm.app),
            {"stanza": self.charm.backup.stanza_name},
        )

        # Test when the unit is the leader and the workload is running.
        _member_started.return_value = True
        with self.harness.hooks_disabled():
            self.harness.set_leader()
        self.assertEqual(
            self.charm.backup.can_use_s3_repository(),
            (False, ANOTHER_CLUSTER_REPOSITORY_ERROR_MESSAGE),
        )
        _update_config.assert_called_once()
        _member_started.assert_called_once()
        _reload_patroni_configuration.assert_called_once()

        # Assert that the stanza name is not present in the unit relation data anymore.
        self.assertEqual(self.harness.get_relation_data(self.peer_rel_id, self.charm.app), {})

        # Test when the workload is not running.
        _update_config.reset_mock()
        _member_started.reset_mock()
        _reload_patroni_configuration.reset_mock()
        _member_started.return_value = False
        with self.harness.hooks_disabled():
            self.harness.update_relation_data(
                self.peer_rel_id,
                self.charm.app.name,
                {"stanza": self.charm.backup.stanza_name},
            )
        self.assertEqual(
            self.charm.backup.can_use_s3_repository(),
            (False, ANOTHER_CLUSTER_REPOSITORY_ERROR_MESSAGE),
        )
        _update_config.assert_called_once()
        _member_started.assert_called_once()
        _reload_patroni_configuration.assert_not_called()

        # Assert that the stanza name is not present in the unit relation data anymore.
        self.assertEqual(self.harness.get_relation_data(self.peer_rel_id, self.charm.app), {})

        # Test when there is no backup from another cluster in the S3 repository.
        with self.harness.hooks_disabled():
            self.harness.update_relation_data(
                self.peer_rel_id,
                self.charm.app.name,
                {"stanza": self.charm.backup.stanza_name},
            )
        _execute_command.return_value = (
            f'[{{"name": "{self.charm.backup.stanza_name}"}}]',
            None,
        )
        self.assertEqual(
            self.charm.backup.can_use_s3_repository(),
            (True, None),
        )

        # Assert that the stanza name is still in the unit relation data.
        self.assertEqual(
            self.harness.get_relation_data(self.peer_rel_id, self.charm.app),
            {"stanza": self.charm.backup.stanza_name},
        )

    def test_construct_endpoint(self):
        pass

    def test_empty_data_files(self):
        pass

    def test_change_connectivity_to_database(self):
        pass

    def test_execute_command(self):
        pass

    def test_format_backup_list(self):
        pass

    def test_generate_backup_list_output(self):
        pass

    def test_list_backups(self):
        pass

    def test_initialise_stanza(self):
        pass

    def test_is_primary_pgbackrest_service_running(self):
        pass

    def test_on_backup_s3_credential_changed(self):
        pass

    def test_on_restore_s3_credential_changed(self):
        pass

    def test_on_create_backup_action(self):
        pass

    def test_on_list_backups_action(self):
        pass

    def test_on_restore_action(self):
        pass

    def test_pre_restore_checks(self):
        pass

    def test_render_pgbackrest_conf_file(self):
        pass

    def test_restart_database(self):
        pass

    def test_retrieve_s3_parameters(self):
        pass

    def test_start_stop_pgbackrest_service(self):
        pass

    def test_upload_content_to_s3(self):
        pass
