#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.


import pytest
from pytest_operator.plugin import OpsTest

from tests.integration.ha_tests.helpers import (
    METADATA,
    app_name,
    get_primary,
    kill_process,
)

# PATRONI_PROCESS = "/usr/bin/python3 /usr/local/bin/patroni /var/lib/postgresql/data/patroni.yml"
POSTGRESQL_PROCESS = "postgres"


@pytest.mark.abort_on_fail
@pytest.mark.ha
async def test_build_and_deploy(ops_test: OpsTest) -> None:
    """Build and deploy three unit of PostgreSQL."""
    # It is possible for users to provide their own cluster for HA testing. Hence, check if there
    # is a pre-existing cluster.
    if await app_name(ops_test):
        return

    charm = await ops_test.build_charm(".")
    async with ops_test.fast_forward():
        await ops_test.model.deploy(
            charm,
            resources={
                "postgresql-image": METADATA["resources"]["postgresql-image"]["upstream-source"]
            },
            num_units=3,
            trust=True,
        )
        await ops_test.model.wait_for_idle(status="active", timeout=1000)


@pytest.mark.ha
async def test_kill_db_processes(ops_test):
    # locate primary unit
    app = await app_name(ops_test)
    primary_name = await get_primary(ops_test, app)

    print(f"primary_name: {primary_name}")
    await kill_process(ops_test, primary_name, POSTGRESQL_PROCESS, kill_code="SIGKILL")
