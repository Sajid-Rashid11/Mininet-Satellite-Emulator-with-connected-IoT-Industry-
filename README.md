# Satellites3 — LEO Satellite Network and Industrial IoT Emulation

**Extended from Jim (James) Wanderer’s [Satellites](https://github.com/jmwanderer/satellites) project.**

This research-oriented repository models LEO satellite motion and emulates a dynamic satellite network using **Skyfield**, **NetworkX**, **Mininet**, and **FRRouting (FRR)**. It extends the original constellation visualizer and routing demonstration with ground-station access, industrial IoT edge hosts, weather-dependent satellite uplinks, make-before-break handovers, traffic experiments, and analysis scripts.

> **Scope of this archive:** The checked-in `mnet/frr_topo.py` implements **IPv4 FRR/OSPF with geometry-based uplink selection**, including a 50 km handoff hysteresis threshold. A separately developed quality-aware OSPF implementation and IPv6/Quagga-MDR OSPF-MANET implementation have been used in the wider research project, but **those updated implementations are not present as the active runtime in this ZIP**. Do not assume that running this archive reproduces quality-aware or OSPF-MANET results without adding the corresponding code.

## 1. What the project does

- **Satellite orbit modeling:** Generate idealized constellations and obtain satellite positions via Skyfield/TLE representations; visualize satellites with Panda3D.
- **Configurable constellation topology:** Generate interconnected orbital rings (a torus-style inter-satellite-link topology) with NetworkX. The included `mnet/configs/small.net` uses **8 rings × 8 satellites (64 total)**; other configuration files support different sizes.
- **Ground stations:** Model geographically located ground stations whose access satellites vary with position, elevation, and time.
- **Live uplink management:** Add/remove Mininet ground-station-to-satellite links in response to visibility updates. Select the closest visible candidate and avoid unnecessary changes using distance hysteresis.
- **Make-before-break handovers:** Establish a candidate link before switching the ground station’s default route, check OSPF adjacency readiness, briefly retain the old link, then remove it. On timeout, discard the attempted link and retain the old path when possible.
- **Weather-induced link degradation:** Read configurable rain cells and estimate Ku-band attenuation using an ITU-R P.838-3-inspired specific-attenuation model plus geometric ray/cylinder intersection. Translate estimated attenuation into emulated bandwidth/loss conditions.
- **Industrial IoT edge connectivity:** Attach an L3 Linux gateway, an Open vSwitch LAN switch, and four IoT hosts to each ground station. The IPv4 implementation configures addressing, forwarding, and NETMAP-based translation for the modeled edge networks.
- **API and monitoring:** Use a FastAPI dashboard and endpoints to inspect the emulation, update links and rain, and inspect monitoring information.
- **Experiment artifacts:** Preserve UDP/TCP `iperf3` logs, ICMP RTT/loss traces, topology-disruption experiments, multi-host runs, and plotting/summary scripts.

## 2. Repository layout

```text
satellites3/
├── README.md                         # Original upstream README (replace with this document if desired)
├── LICENSE                           # Upstream MIT license
├── requirements.txt                  # Base visualization/orbit dependencies
├── animate_orbits.py                 # Idealized Panda3D constellation visualization
├── orbit_set.py                      # TLE-based satellite-group visualization
├── sat_pos_samples.py                # Satellite position samples
├── torus_topo.py                     # Constellation topology and ground-station definitions
├── frr_config_topo.py                # FRR configuration generation
├── geosimsat.py                      # Satellite physics / link-visibility update process
├── simapi.py / simclient.py          # API data types and client
├── rain_cells.json                   # Geographic rain-cell scenarios
├── kill_satellite.py                 # Toggle a satellite's inter-satellite links via API
├── mnet/
│   ├── README.md                     # Original Mininet-specific notes
│   ├── requirements.txt
│   ├── run_mn.py                      # Main emulation entry point
│   ├── frr_topo.py                    # Active IPv4 FRR/OSPF topology and runtime
│   ├── frr_topo_old.py                # Older/alternate runtime version
│   ├── run_mn_constellation_only.py   # Alternate constellation-only entry point
│   ├── frr_topo_constellation_only.py
│   ├── driver.py                      # FastAPI server, dashboard, monitoring/weather updates
│   ├── itu_p838.py                    # Rain attenuation / storm geometry
│   ├── pmonitor.py                    # Connectivity monitoring
│   └── configs/                       # *.net topology settings
├── results/                           # Experimental logs, plotting and analysis scripts
└── final_results_udp/                 # Additional UDP experiment logs and analysis
```

The uploaded archive also contains a `venv/` directory and many generated outputs; these are **local artifacts**, not source dependencies to commit to a public repository. Large run logs and figures are best distributed separately or in a clearly named `results/` release.

## 3. Requirements

Use **Linux** with Mininet, Open vSwitch, and FRRouting installed for full emulation. The included implementation assumes an FRR installation with `vtysh` and the `ospfd`, `zebra`, and `staticd` components. The Mininet setup creates an OVS bridge and uses a **remote OpenFlow controller at `127.0.0.1:6653`**; provide a compatible controller or adapt the Mininet controller setup before expecting switch forwarding to work.

Python dependencies documented in the repository include:

- `panda3D`, `networkx`, `skyfield` (`requirements.txt`)
- `fastapi`, `requests`, `mininet` (`mnet/requirements.txt`)

The source additionally imports `uvicorn`, `jinja2`, and `pydantic`; install these if not already available. For the analysis scripts, install `matplotlib`, and any other packages imported by the particular plot/metrics script. Mininet’s Python module must be accessible to the Python interpreter used for the emulation (system packages may not automatically be visible from a virtual environment).

### Python environment

From the repository root:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -r mnet/requirements.txt
pip install uvicorn jinja2 pydantic matplotlib
```

> **Note:** Mininet is usually installed through Linux packages or its upstream installation procedure, not solely through `pip`. Running a virtual-environment Python under `sudo` requires an explicit interpreter path or appropriate environment handling. Ensure the FRR and Open vSwitch services are installed and configured for your host. These instructions are a setup guide, not a claim that this archive was tested on every distribution or package version.

## 4. Configure a constellation

Configuration files reside in `mnet/configs/`. For example, `mnet/configs/small.net` contains:

```ini
[network]
rings=8
routers=8
ground_stations=yes

[monitor]
stable_monitors=yes

[physical]
min_altitude=10
```

- `rings`: number of orbital rings.
- `routers`: satellites per ring.
- `ground_stations`: include terrestrial gateway sites.
- `stable_monitors`: enable monitoring of nodes expected to be persistently reachable.
- `min_altitude`: minimum satellite elevation angle in degrees, read by `geosimsat.py`.

**Keep the same `.net` file in both emulation and orbital-simulation processes.** The archive includes `small.net` (8×8) and `large.net` (16×16), along with other sample configs. Large configurations may require considerable CPU and memory resources.

## 5. Start the Mininet emulation

From the repository root, start the Mininet/FRR process in **Terminal 1** (using a Python interpreter that can import Mininet):

```bash
sudo python3 -m mnet.run_mn mnet/configs/small.net
```

This creates the satellite nodes, inter-satellite links, ground-station sites, gateway/switch/IoT components, FRR routing processes, monitoring, and the control API.

Then launch the satellite-physics process in **Terminal 2**:

```bash
python3 geosimsat.py mnet/configs/small.net
```

The physics process periodically computes positions and visibility and submits uplink and ISL-state updates to the API. The source defines a **10-second physics time slice**; note that this is a topology-update interval, not necessarily the actual period at which each OSPF adjacency reaches `Full`.

Open the dashboard at:

```text
http://localhost:8000
```

The API is also available through the same host and port. A remote browser must use the host or VM’s reachable address, subject to networking/firewall settings.

### Alternative CLI mode

```bash
sudo python3 -m mnet.run_mn mnet/configs/small.net --cli
```

`run_mn.py` also recognizes `--no-mnet`, but in **this archived version** the main `run()` function still constructs and starts Mininet, so do **not** rely on that option for a no-Mininet stub without fixing the implementation first.

### Check that the emulation is running

```bash
curl http://127.0.0.1:8000/
curl http://127.0.0.1:8000/rain/status
```

For debugging, inspect the routing states in the appropriate FRR namespace, and verify that the new GS–SAT interface reaches OSPF `Full` before a handoff switches the active route. Depending on local FRR setup, use the `vtysh -N <node>` convention employed in `mnet/frr_topo.py`.

## 6. How ground-station uplinks and handovers work

The orbital process (`geosimsat.py`) computes elevation, azimuth, and distance for satellites above the configured minimum elevation. It sends **up to two closest visible satellites** for each ground station to the Mininet API. The active runtime (`mnet/frr_topo.py`) then determines the preferred uplink:

1. **Select:** choose the closest satellite from the supplied visible candidates.
2. **Hysteresis:** if the current satellite remains visible, switch only when another candidate is at least **50 km closer** (based on current geometry).
3. **Make:** create the new Mininet GS–SAT link with an allocated IPv4 `/30`, activate OSPF, and retain the current uplink.
4. **Wait:** check that the new ground-station OSPF interface has a `Full` neighbor.
5. **Switch:** install a default route through the new satellite while preserving the old link.
6. **Guard interval:** keep the previous link for **3 seconds** before deleting it.
7. **Timeout/fallback:** if readiness is not achieved within **20 seconds**, remove the attempted new link and retain the old path if it remains visible.

If a previously active satellite falls outside the current visible set, its uplink may be deleted as stale; consequently, make-before-break **does not guarantee zero loss of connectivity** when visibility disappears before a replacement path is ready.

These numeric values describe **the code in this ZIP**, not necessarily the later quality-aware implementation used in research experiments. The IPv4 runtime contains an `ip_pool` for dynamic GS–SAT addresses and FRR OSPFv2 configuration.

## 7. Rain attenuation and weather updates

The `rain_cells.json` file specifies storm cells with fields such as:

```json
{
  "name": "Sydney_Offshore_Storm",
  "lat": -33.90,
  "lon": 151.45,
  "radius_km": 25.0,
  "height_km": 4.5,
  "rain_rate_mmh": 0.0
}
```

The included configuration contains storm cells near **Sydney** and **Zurich**. Set `rain_rate_mmh` to the desired experiment condition before starting, or update a named cell while the API is running. For example:

```bash
curl -X PUT 'http://127.0.0.1:8000/rain?cell_name=Sydney_Offshore_Storm&rain_rate=15'
curl http://127.0.0.1:8000/rain/status
curl -X PUT http://127.0.0.1:8000/weather/update
```

The attenuation module uses an approximation of the ITU-R P.838-3 model with **12 GHz** coefficient settings and estimates the distance a GS–satellite ray travels through a cylindrical storm volume. The emulator then maps the resulting attenuation to `TCLink` bandwidth and loss parameters. This bandwidth/loss mapping is an **emulation heuristic**, not a full radio link-budget or physical-layer modem simulation.

`mnet/driver.py` also reevaluates monitoring and weather periodically (approximately every **2 seconds** in its background loop). The orbital geometry itself updates on a different time step.

## 8. Industrial IoT traffic setup

When ground stations are enabled, the topology creates the following structure for each site:

```text
IoT host 0 ─┐
IoT host 1 ─┤
IoT host 2 ─┼─ OVS LAN switch ─ Linux L3 gateway ─ Ground station ─ LEO uplink
IoT host 3 ─┘
```

The IPv4 implementation assigns private LAN addresses, a gateway–ground-station point-to-point subnet, and a translated prefix for inter-site traffic. Traffic can cross the constellation via the GS uplinks and ISLs, provided that FRR routes, gateway forwarding, and OpenFlow switching are operating correctly.

To measure throughput and reliability, run tools such as `iperf3` and `ping` **inside the appropriate Mininet hosts/namespaces**. The repository’s `results/` directories contain examples of captured client/server logs and ICMP measurements; those output files are not automatically generated by merely starting the two main emulation processes.

## 9. Controlled topology disruptions

The included helper can take a satellite’s **inter-satellite links** down or restore them through the API:

```bash
python3 kill_satellite.py R3_5 down
python3 kill_satellite.py R3_5 up
```

The script uses an **8×8** ring indexing assumption. Update `NUM_RINGS` and `NUM_PER_RING` in `kill_satellite.py` for other constellation sizes. The helper targets ISLs; it does not directly remove every ground-station connection to the named satellite.

## 10. Experiments and result analysis

The archive includes data and analysis scripts for:

| Experiment family | Typical analysis |
| --- | --- |
| Clear-sky and rain-rate sweeps | UDP/TCP server- and client-side bitrate, windowed time series |
| Handover and satellite/ISL disruption | ICMP delivery/loss/RTT, UDP/TCP throughput time series |
| Multi-host/multi-flow scenarios | Per-flow throughput and Jain’s fairness index |
| Outage analysis | Fraction of time or intervals below a chosen throughput threshold |

Look under `results/` and `final_results_udp/` for experiment folders, log files, plotting utilities, and already generated PNG/PDF figures. Example output conventions include:

```text
results/ospf/clear/throughput/udp/server.txt
results/ospf/clear/throughput/tcp/client.txt
results/ospf/rain_syd_offshore_15mmh/throughput/udp/server_corrected.txt
results/ospf/sat_down_syd_clear/throughput/udp/ping.txt
results/ospf/multi_host/throughput/udp/4x4/...
```

The `corrected` log variants represent separately retained experiment artifacts; **inspect a particular script’s file-selection logic** to determine which log is used for any specific figure. Avoid mixing old and updated experiments without explicitly labeling the routing policy and configuration used for each run.

### Running a plotting script

Plotting utilities differ in their interfaces. Some use fixed paths; others require a data-directory positional argument and support flags such as `--window` and `-o`. Read `--help` for the chosen script before running it. For example, for a script that accepts an input directory:

```bash
python3 path/to/plot_udp_rain_overlay_windowed.py results \
  --window 10.0 \
  -o results/udp_rain_overlay_avg10.pdf
```

The script above is **an example invocation pattern**, not a claim that this exact file path exists in the archive. For publication figures, ensure consistent time windows, axis units, labels, markers, and legend sizing. Preserve source logs for reproducibility.

## 11. Original visualization and topology tools

The original project utilities remain useful independently of Mininet:

```bash
python3 animate_orbits.py
python3 orbit_set.py starlink
python3 sat_pos_samples.py
python3 torus_topo.py
python3 frr_config_topo.py
```

The original `orbit_set.py` also supports satellite groups such as `stations`, `kuiper`, `GPS`, and an artificial constellation; visualization speed depends on the machine’s CPU/GPU. See the original upstream documentation for the full interactive keyboard controls.

## 12. Differences from the original Wanderer repository

| Area | Original project | Extensions present in this archive |
| --- | --- | --- |
| Orbital modeling | Skyfield, Panda3D, ideal and TLE-style constellations | Retained and used as a physical state driver for the network |
| Routing testbed | Mininet/FRR satellite routing demonstration | Expanded ground-station access and runtime handoff handling |
| Topology sizing | Small default demo and configurable rings | Included 8×8 example and larger `.net` configurations |
| GS connectivity | Original README listed several GS/host features as future work | Dynamic GS–SAT link creation/removal and route updates implemented |
| IoT endpoints | Future extension in original README | Gateway, Open vSwitch, and four IoT hosts per GS |
| Weather | Not described in original README | Storm configuration, path intersection, attenuation-to-link-conditions model |
| Resilience tests | Basic status monitoring | Explicit ISL failure helper, handoff and topology-disruption experiments |
| Measurement | API/ping connectivity monitoring | Saved UDP, TCP, ICMP, multi-flow, outage and fairness experiments |
| Quality-aware selection | Not part of original release | **Research extension developed separately; not present in active ZIP runtime** |
| OSPF-MANET | Not described as included | **Research extension developed separately; not present in active ZIP runtime** |

## 13. Known limitations and reproducibility notes

1. The bundled active runtime is an **IPv4 FRR/OSPF geometry-based baseline**, not the later QA/OSPF-MANET version. Update the tracked code and this README together when combining repositories.
2. `run_mn.py` configures a remote OpenFlow controller; an absent controller may prevent switching even though Mininet creates the nodes.
3. The `--no-mnet` flag is parsed but not honored by the archived main `run()` implementation.
4. The make-before-break readiness check uses GS-side OSPF state; it is not an end-to-end application-level traffic probe.
5. A handoff timeout or abrupt loss of satellite visibility can still cause outages. Results depend on orbit geometry, timers, topology-update frequency, available resources, and routing convergence.
6. The rain mapping estimates link impairment; it does not model all satellite RF effects, antenna behavior, queue dynamics, or channel coding.
7. Several shell/API helpers assume an 8×8 topology, particular node names, or standard FRR/Mininet paths. Adapt those assumptions to different systems.
8. The archive includes a local `venv/` and extensive logs. Do not redistribute the virtual environment or assume that its binary packages are portable.
9. For rigorously reproducible results, document the run duration, source-code revision, physical host, controller, rain scenario, traffic rate, client/server pair, number of repetitions, and how raw logs were selected/averaged.

## 14. Upstream attribution and license

The original **Satellites** repository was created by **Jim Wanderer** and is distributed under the **MIT License**. This project extends that code for research into LEO/non-terrestrial network emulation, industrial IoT connectivity, routing resilience, and weather-aware uplink behavior.

- Original repository: https://github.com/jmwanderer/satellites
- Original project article: https://medium.com/@jmwanderer/simulating-satellite-networks-897dd3cb1468
- Upstream license: see [`LICENSE`](LICENSE) (copyright © 2024 Jim Wanderer)

Retain the original copyright and license text when publishing modified source. Add maintainer identities, research-paper citations, and reproducibility metadata when this extension is released publicly.
