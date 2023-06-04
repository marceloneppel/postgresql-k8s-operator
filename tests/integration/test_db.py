#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
from asyncio import gather

from pytest_operator.plugin import OpsTest

from tests.integration.helpers import (
    DATABASE_APP_NAME,
    build_and_deploy,
    wait_for_relation_removed_between,
)

EXTENSIONS_BLOCKING_MESSAGE = "extensions requested through relation"
FINOS_WALTZ_APP_NAME = "finos-waltz"
ANOTHER_FINOS_WALTZ_APP_NAME = "another-finos-waltz"
APPLICATION_UNITS = 1
DATABASE_UNITS = 3

logger = logging.getLogger(__name__)


async def test_indico_db_blocked(ops_test: OpsTest) -> None:
    """Tests if deploying and relating to Indico charm will block due to requested extensions."""
    async with ops_test.fast_forward(fast_interval="30s"):
        # Build and deploy the PostgreSQL charm (use a custom name until
        # https://warthogs.atlassian.net/browse/DPE-2000 is solved).
        database_application_name = f"extensions-{DATABASE_APP_NAME}"
        await build_and_deploy(ops_test, 1, database_application_name)

        await ops_test.model.deploy(
            "indico",
            channel="stable",
            application_name="indico1",
            num_units=APPLICATION_UNITS,
        )
        await ops_test.model.deploy(
            "indico",
            channel="stable",
            application_name="indico2",
            num_units=APPLICATION_UNITS,
        )
        await ops_test.model.deploy("redis-k8s", channel="stable", application_name="redis-broker")
        await ops_test.model.deploy("redis-k8s", channel="stable", application_name="redis-cache")
        await gather(
            ops_test.model.relate("redis-broker", "indico1"),
            ops_test.model.relate("redis-cache", "indico1"),
        )

        # Wait for model to stabilise
        await ops_test.model.wait_for_idle(
            apps=["indico1", "indico2"],
            status="waiting",
            raise_on_blocked=False,
            timeout=1000,
        )
        unit = ops_test.model.units.get("indico1/0")
        ops_test.model.block_until(
            lambda: unit.workload_status_message == "Waiting for database availability",
            timeout=1000,
        )

        await gather(
            ops_test.model.relate(f"{database_application_name}:db", "indico1:db"),
            ops_test.model.relate(f"{database_application_name}:db", "indico2:db"),
        )

        await ops_test.model.wait_for_idle(
            apps=[database_application_name],
            status="blocked",
            raise_on_blocked=False,
            timeout=1000,
        )

        assert (
            ops_test.model.applications[database_application_name].units[0].workload_status_message
            == EXTENSIONS_BLOCKING_MESSAGE
        )

        await ops_test.model.applications[database_application_name].destroy_relation(
            f"{database_application_name}:db", "indico1:db"
        )

        await ops_test.model.wait_for_idle(
            apps=[database_application_name],
            status="blocked",
            raise_on_blocked=False,
            timeout=1000,
        )

        # Verify that the charm remains blocked if there are other blocking relations
        assert (
            ops_test.model.applications[database_application_name].units[0].workload_status_message
            == EXTENSIONS_BLOCKING_MESSAGE
        )

        await ops_test.model.applications[database_application_name].destroy_relation(
            f"{database_application_name}:db", "indico2:db"
        )

        # Verify that active status is restored when all blocking relations are gone
        await ops_test.model.wait_for_idle(
            apps=[database_application_name],
            status="active",
            raise_on_blocked=False,
            timeout=1000,
        )

        # Verify that the charm doesn't block when the extensions are enabled.
        logger.info("Verifying that the charm doesn't block when the extensions are enabled")
        config = {"plugin_pg_trgm_enable": "True", "plugin_unaccent_enable": "True"}
        await ops_test.model.applications[database_application_name].set_config(config)
        await ops_test.model.wait_for_idle(
            apps=[database_application_name], status="active", idle_period=15
        )
        await ops_test.model.relate(f"{database_application_name}:db", "indico1:db")
        await ops_test.model.wait_for_idle(
            apps=[database_application_name, "indico1"],
            status="active",
            raise_on_blocked=False,
            timeout=2000,
        )

        # Verify that the charm unblocks when the extensions are enabled after being blocked
        # due to disabled extensions.
        logger.info("Verifying that the charm unblocks when the extensions are enabled")
        config = {"plugin_pg_trgm_enable": "False", "plugin_unaccent_enable": "False"}
        await ops_test.model.applications[database_application_name].set_config(config)
        await ops_test.model.applications[database_application_name].destroy_relation(
            f"{database_application_name}:db", "indico1:db"
        )
        wait_for_relation_removed_between(ops_test, database_application_name, "indico1")
        await ops_test.model.wait_for_idle(
            apps=[database_application_name], status="active", idle_period=15, timeout=2000
        )

        await ops_test.model.relate(f"{database_application_name}:db", "indico1:db")
        unit = next(iter(ops_test.model.units.values()))
        ops_test.model.block_until(
            lambda: unit.workload_status_message == EXTENSIONS_BLOCKING_MESSAGE, timeout=600
        )

        config = {"plugin_pg_trgm_enable": "True", "plugin_unaccent_enable": "True"}
        await ops_test.model.applications[database_application_name].set_config(config)
        await ops_test.model.wait_for_idle(
            apps=[database_application_name, "indico1"],
            status="active",
            raise_on_blocked=False,
            timeout=2000,
            idle_period=15,
        )

        # Cleanup
        await gather(
            ops_test.model.remove_application(database_application_name, block_until_done=True),
            ops_test.model.remove_application("indico1", block_until_done=True),
            ops_test.model.remove_application("indico2", block_until_done=True),
            ops_test.model.remove_application("redis-broker", block_until_done=True),
            ops_test.model.remove_application("redis-cache", block_until_done=True),
        )
