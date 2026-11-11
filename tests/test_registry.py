from ts.registry import ACTIONS, ALWAYS_OUTCOMES, CHECKS, Param

REAL_CHECKS = {"snmp_saw_down", "traffic_stopped", "device_log", "interface_status",
               "topology_peer", "device_up"}


def test_param_parsing():
    assert Param.parse("device") == Param("device", False, False)
    assert Param.parse("lines[]") == Param("lines", True, False)
    assert Param.parse("source?") == Param("source", False, True)


def test_blocks_are_registered():
    assert REAL_CHECKS <= set(CHECKS)
    for block in CHECKS.values():
        assert block.outcomes, block.name
        assert not set(block.outcomes) & set(ALWAYS_OUTCOMES), block.name
    for block in ACTIONS.values():
        assert block.undo in ("auto", "explicit", "not_needed"), block.name


def test_real_checks_say_what_they_do():
    for name in REAL_CHECKS:
        assert CHECKS[name].description and CHECKS[name].func, name


def test_host_checks_are_not_device_bound():
    assert not CHECKS["topology_peer"].device_bound
    assert not CHECKS["snmp_saw_down"].device_bound
    assert CHECKS["interface_status"].device_bound
