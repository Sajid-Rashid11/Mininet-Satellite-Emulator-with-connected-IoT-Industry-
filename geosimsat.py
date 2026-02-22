"""
Geographic Satellite Simulator
Simulate location changes in a satellite network in real time.

Simulate in real time specific events in a satellite network:
Generate events for:
    - Satellite position - based on TLE data specs
    - horizontal links down above and below a critical latitude
    - new / break  connections to ground stations
    - new / break connections to end hosts
"""

from dataclasses import dataclass, field
import configparser
import sys
import datetime
import time

import torus_topo
import simclient

import networkx
from skyfield.api import load, wgs84 # type: ignore
from skyfield.api import EarthSatellite # type: ignore
from skyfield.positionlib import Geocentric # type: ignore
from skyfield.toposlib import GeographicPosition # type: ignore
from skyfield.units import Angle, Distance # type: ignore


@dataclass
class Satellite:
    """Represents an instance of a satellite"""

    name: str
    earth_sat: EarthSatellite
    geo: Geocentric = None
    lat: Angle = 0
    lon: Angle = 0
    height: Distance = 0
    inter_plane_status: bool = True
    prev_inter_plane_status: bool = True

@dataclass
class Uplink:
    satellite_name: str
    ground_name: str
    distance: int
    az_deg: float = 0.0  # [NEW]
    el_deg: float = 0.0  # [NEW]
@dataclass
class GroundStation:
    """Represents an instance of a ground station"""
    name: str
    position: GeographicPosition
    uplinks: list[Uplink] = field(default_factory=list)


class SatSimulation:
    """
    Runs real time to update satellite positions
    """

    # Time slice for simulation
    TIME_SLICE = 10
    MIN_ALTITUDE = 10

    def __init__(self, graph: networkx.Graph):
        self.graph = graph
        self.ts = load.timescale()
        self.satellites: list[Satellite] = []
        self.ground_stations: list[GroundStation] = []
        self.client: simclient.Client = simclient.Client("http://127.0.0.1:8000")
        self.calc_only = False
        self.min_altitude = SatSimulation.MIN_ALTITUDE
        self.zero_uplink_count = 0
        self.uplink_updates = 0

        for name in torus_topo.ground_stations(graph):
            node = graph.nodes[name]
            position = wgs84.latlon(node[torus_topo.LAT], node[torus_topo.LON])
            ground_station = GroundStation(name, position)
            self.ground_stations.append(ground_station)

        for name in torus_topo.satellites(graph):
            orbit = graph.nodes[name]["orbit"]
            ts = load.timescale()
            l1, l2 = orbit.tle_format()
            earth_satellite = EarthSatellite(l1, l2, name, ts)
            satellite = Satellite(name, earth_satellite)
            self.satellites.append(satellite)

    def updatePositions(self, future_time: datetime.datetime):
        sfield_time = self.ts.from_datetime(future_time)
        for satellite in self.satellites:
            satellite.geo = satellite.earth_sat.at(sfield_time)
            lat, lon = wgs84.latlon_of(satellite.geo)
            satellite.lat = lat
            satellite.lon = lon
            satellite.height = wgs84.height_of(satellite.geo)
            #print(f"{satellite.name} Lat: {satellite.lat}, Lon: {satellite.lon}, Hieght: {satellite.height.km}km")

    @staticmethod
    def nearby(ground_station: GroundStation, satellite: Satellite) -> bool:
        return (satellite.lon.degrees > ground_station.position.longitude.degrees - 20 and
                satellite.lon.degrees < ground_station.position.longitude.degrees + 20 and
                satellite.lat.degrees > ground_station.position.latitude.degrees - 20 and 
                satellite.lat.degrees < ground_station.position.latitude.degrees + 20)
 
    def updateUplinkStatus(self, future_time: datetime.datetime):
        self.uplink_updates += 1
        sfield_time = self.ts.from_datetime(future_time)
        zero_uplinks_count = 0

        for ground_station in self.ground_stations:
            visible_candidates = []

            # 1. Find ALL Visible Candidates (Strict Physics)
            for satellite in self.satellites:
                difference = satellite.earth_sat - ground_station.position
                topocentric = difference.at(sfield_time)
                alt, az, d = topocentric.altaz()

                dist_km = d.km
                el_deg = alt.degrees
                az_deg = az.degrees

                # STRICT: Only consider satellites above minimum altitude
                # No more negative elevation fallbacks!
                if el_deg > self.min_altitude:
                    visible_candidates.append((dist_km, satellite, az_deg, el_deg))

            # 2. Select Top 2 Winners
            ground_station.uplinks = [] # Reset local state
            
            # Sort by distance (closest first)
            visible_candidates.sort(key=lambda x: x[0])
            
            # Pick up to 2 satellites
            selected_candidates = visible_candidates[:2]

            if not selected_candidates:
                zero_uplinks_count += 1
                continue

            # 3. Update Local State & Prepare Payload
            uplink_payload = []
            
            for dist, sat, az, el in selected_candidates:
                # Add to Local Object (Ensure your Uplink class in this file has these fields!)
                ground_station.uplinks.append(
                    Uplink(sat.name, ground_station.name, int(dist), float(az), float(el))
                )
                
                # Add to API Payload
                uplink_payload.append({
                    "sat_node": sat.name,
                    "distance": int(dist),
                    "az_deg": float(az),
                    "el_deg": float(el),
                })

            # 4. Send Update to Mininet
            try:
                self.client.set_uplinks(
                    ground_node=ground_station.name,
                    gs_lat=ground_station.position.latitude.degrees,
                    gs_lon=ground_station.position.longitude.degrees,
                    uplinks=uplink_payload
                )
            except Exception as e:
                print(f"Error updating {ground_station.name}: {e}")

        if zero_uplinks_count > 0:
            self.zero_uplink_count += zero_uplinks_count
            

    def updateInterPlaneStatus(self):
        inclination = self.graph.graph["inclination"]
        for satellite in self.satellites:
            # Track if state changed
            satellite.prev_inter_plane_status = satellite.inter_plane_status
            if satellite.lat.degrees > (inclination - 2) or satellite.lat.degrees < (
                -inclination + 2
            ):
                # Above the threashold for inter plane links to connect
                satellite.inter_plane_status = False
            else:
                satellite.inter_plane_status = True

    def send_updates(self):
        for satellite in self.satellites:
            if satellite.prev_inter_plane_status != satellite.inter_plane_status:
                for neighbor in self.graph.adj[satellite.name]: 
                    if self.graph.edges[satellite.name, neighbor]["inter_ring"]:
                        self.client.set_link_state(satellite.name, neighbor, satellite.inter_plane_status)
        

    def run(self):
        current_time = datetime.datetime.now(tz=datetime.timezone.utc)
        slice_delta = datetime.timedelta(seconds=SatSimulation.TIME_SLICE)

        # Generate positions for current time
        print(f"update positions for {current_time}")
        self.updatePositions(current_time)
        self.updateUplinkStatus(current_time)
        self.updateInterPlaneStatus()
        self.send_updates()

        while True:
            # Generate positions for next time step
            future_time = current_time + slice_delta
            print(f"update positions for {future_time}")
            self.updatePositions(future_time)
            self.updateUplinkStatus(future_time)
            self.updateInterPlaneStatus()
            sleep_delta = future_time - datetime.datetime.now(tz=datetime.timezone.utc)
            print(f"zero uplink % = {self.zero_uplink_count / self.uplink_updates}")
            print("sleep")
            if not self.calc_only:
                # Wait until next time step thenupdate
                time.sleep(sleep_delta.seconds)
                self.send_updates()
            current_time = future_time


def run(num_rings: int, num_routers: int, ground_stations: bool, min_alt: int, calc_only: bool) -> None:
    """
    Simulate physical positions of satellites.

    num_rings: number of orbital rings
    num_routers: number of satellites on each ring
    ground_stations: True if groundstations are included
    min_alt: Minimum angle (degrees) above horizon needed to connect to the satellite
    calc_only: If True, only loop quicky dumping results to the screen
    """
    graph = torus_topo.create_network(num_rings, num_routers, ground_stations)
    sim: SatSimulation = SatSimulation(graph)
    sim.min_altitude = min_alt
    sim.calc_only = calc_only
    sim.run()


def usage():
    print("Usage: sim_sat [config-file] [--calc-ony]")

if __name__ == "__main__":
    calc_only = False
    if "--calc-only" in sys.argv:
        # Only run calculations in a loop reporting data to the screen
        calc_only = True
        sys.argv.remove("--calc-only")

    if len(sys.argv) > 2:
        usage()
        sys.exit(-1)
        
    parser = configparser.ConfigParser()
    parser['network'] = {}
    parser['physical'] = {}
    try:
        if len(sys.argv) == 2:
            parser.read(sys.argv[1])
    except Exception as e:
        print(str(e))
        usage()
        sys.exit(-1)

    num_rings = parser['network'].getint('rings', 4)
    num_routers = parser['network'].getint('routers', 4)
    # Should ground stations be included in the network?
    ground_stations = parser['network'].getboolean('ground_stations', False)
    # Minimum angle above horizon needed to connect to satellites
    min_alt = parser['physical'].getint('min_altitude', SatSimulation.MIN_ALTITUDE)

    print(f"Running {num_rings} rings with {num_routers} per ring, ground stations {ground_stations}")
    run(num_rings, num_routers, ground_stations, min_alt, calc_only)
