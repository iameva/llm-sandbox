# Legacy: container-era plans and probes

These files belong to the Podman container workflow (container, gVisor and
krun modes), which the QEMU image replaced. They record what was tried and
measured; they are not current guarantees. Paths inside them still read as
they did before the move (`tests/host_assumptions.py` is now
`legacy/host_assumptions.py`).

- `vm-migration-plan.md`: krun and gVisor experiments.
- `network-restriction-plan.md`: the host egress plan and its probe results.
- `host_assumptions.py`: the host probe that plan's results came from; needs Podman.
- `host_uds_boundary.py`: gVisor Unix-socket isolation test; needs Podman and runsc.
