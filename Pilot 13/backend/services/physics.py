"""Physical validity enforcement for the VRP system.

P0 rules — violations raise AssertionError; no exception suppression allowed.
All rules enforce hard physical constraints that no solver relaxation may override.
"""


def validate_move(move) -> None:
    """P0-1: No self-loops.  P0-2: No fake __transit containers."""
    assert move.from_site_id != move.to_site_id, (
        f"[PHYSICS-P0-1] Self-loop rejected: container {move.bay_id!r}"
        f" move from_site == to_site == {move.from_site_id!r}"
    )
    assert "__transit" not in move.bay_id, (
        f"[PHYSICS-P0-2] Fake container rejected: bay_id={move.bay_id!r}"
        f" (contains '__transit') — only real bay IDs are permitted"
    )


def apply_move(container_id: str, current_location: str, from_loc: str, to_loc: str) -> str:
    """P0-3: Container must be at from_loc before moving.  Returns new location."""
    assert current_location == from_loc, (
        f"[PHYSICS-P0-3] Container {container_id!r} is at {current_location!r},"
        f" not at expected from_loc={from_loc!r} — move rejected"
    )
    return to_loc


def validate_state(
    container_ids: list,
    before_kg: float,
    after_kg: float,
    produced: float,
    consumed: float,
) -> None:
    """P0-3: No duplicate container IDs.  P0-4: Conservation of mass.

    Args:
        container_ids: Flat list of all container/bay IDs in the system.
        before_kg: Total gas mass (kg) before the operation.
        after_kg: Total gas mass (kg) after the operation.
        produced: Gas mass added by production during the operation.
        consumed: Gas mass removed by consumption during the operation.
    """
    # P0-3: uniqueness — no container may appear in two places simultaneously
    assert len(set(container_ids)) == len(container_ids), (
        "[PHYSICS-P0-3] Duplicate container IDs detected:"
        " {}".format(
            sorted({cid for cid in container_ids if container_ids.count(cid) > 1})
        )
    )
    # P0-4: conservation of mass — gas is neither created nor destroyed
    expected = before_kg + produced - consumed
    diff = abs(expected - after_kg)
    assert diff < 1e-3, (
        f"[PHYSICS-P0-4] Mass not conserved:"
        f" before={before_kg:.6f} + produced={produced:.6f} - consumed={consumed:.6f}"
        f" = expected={expected:.6f} != after={after_kg:.6f}"
        f" (diff={diff:.9f} >= 1e-3)"
    )
