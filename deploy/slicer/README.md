# BFF slicer container

OrcaSlicer (pinned in `provision.sh`, `ORCA_VERSION`) behind `slicerd.py`, so Print now works without an agent.

Run on the Proxmox host, from a folder with `provision.sh`, `bff-slicer.service`, `tools/slicer/slicerd.py`, `tools/slicer/flatten_profiles.py`:

    bash provision.sh <ctid> <ip/cidr> <gateway> <bff_ct_ip> [bff_ctid]

- Only the BFF container reaches port 8765 (nftables in the slicer CT).
- The shared secret is generated in the slicer CT (`/etc/bff-slicer.env`, 0600) and, with `bff_ctid`, written into BFF's `/etc/bff-mcp.env` as `BFF_SLICER_URL` / `BFF_SLICER_SECRET`. It is never printed.
- Profiles: official BBL profiles of the same OrcaSlicer tag, flattened (inherits + include templates, so each printer keeps its own start/end G-code) into `/opt/slicer/profiles` with `index.json`.
- Headless OrcaSlicer 2.5 writes no plate picture; time and grams come from `plate_N.statistics.json` (`summary.time`, `summary.weight`).
