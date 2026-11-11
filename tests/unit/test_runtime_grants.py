"""Privilege matrix for the shared API state tables (pure function, no database)."""

import pytest

from infra.scripts.w4_database import API_STATE, PRIVATE, TABLES, rights


def test_api_state_tables_exist_and_are_private():
    assert API_STATE <= TABLES
    assert API_STATE <= PRIVATE


@pytest.mark.parametrize("table", sorted(API_STATE))
def test_only_the_api_reads_and_writes_in_flight_state(table):
    assert rights("api", table) == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    # Collectors and importers never see user conditions or origins.
    assert rights("collector", table) == set()
    assert rights("import", table) == set()
