import os
import grp
import pwd
import ipaddress
import tempfile
import datetime
import shutil
import random
import socket
import typing
from dataclasses import dataclass, field

import networkx
import mininet.topo
import mininet.node
import mininet.net
import mininet.link
import mininet.util

import torus_topo
import frr_config_topo
import simapi
import mnet.pmonitor
import time

from mininet.node import OVSKernelSwitch
from mininet.link import TCLink

from mnet.itu_p838 import RainModel


class RouteNode(mininet.node.Node):
    """
    Mininet node with a loopback.
    Supports FrrRouters and ground sations.

    Includes an optional loopback interface with a /31 subnet mask
    """

    def __init__(self, name, **params):
        mininet.node.Node.__init__(self, name, **params)

        # Optional loopback interface
        self.loopIntf = None

    def defaultIntf(self):
        # If we have a loopback, that is the default interface.
        # Otherwise use mininet default behavior.
        if self.loopIntf is not None:
            return self.loopIntf
        return super().defaultIntf()

    def config(self, **params):
        # If we have a default IP and it is not an existing interface, create a
        # loopback.
        if params.get("ip") is not None:
            match_found = False
            ip = format(ipaddress.IPv4Interface(params.get("ip")).ip)
            for intf in self.intfs.values():
                if intf.ip == ip:
                    match_found = True
            if not match_found:
                # Make a default interface
                mininet.util.quietRun("ip link add name loop type dummy")
                self.loopIntf = mininet.link.Intf(name="loop", node=self)

        super().config(**params)

    def setIP(self, ip):
        # What is this for?
        mininet.node.Node.setIP(self, ip)



class MNetNodeWrap:
    """
    """

    def __init__(self, name : str, default_ip: str) -> None:
        self.name : str = name
        self.default_ip : str = default_ip
        self.node : mininet.node.Node = None
        fd, self.working_db = tempfile.mkstemp(suffix=".sqlite")
        open(fd, "r").close()
        print(f"{self.name} db file {self.working_db}")
        self.last_five_pings = []
 
    def sendCmd(self, command :str):
        if self.node is not None:
            self.node.sendCmd(command)

    def start(self, net: mininet.net.Mininet) -> None:
        """
        Will be called after the mininet node has started
        """
        self.node = net.getNodeByName(self.name)

    def waitOutput(self) -> None:
        if self.node is not None:
            self.node.waitOutput()

    def stop(self) -> None:
        """
        Will be called before the mininet node has stoped
        """
        pass

    def startMonitor(self, db_master_file, db_master):
        print(f"start monitor {self.name}:{self.defaultIP()}")
        self.sendCmd(
            f"python3 -m mnet.pmonitor monitor '{db_master_file}' '{self.working_db}' {self.defaultIP()} >> /dev/null 2>&1  &"
        )
        mnet.pmonitor.set_running(db_master, self.defaultIP(), True)

    def stopMonitor(self, db_master):
        mnet.pmonitor.set_can_run(db_master, self.defaultIP(), False)
        os.unlink(self.working_db)

    def update_monitor_stats(self):
        # Only get stats if DB is being used
        if os.path.getsize(self.working_db) > 0:
            db = mnet.pmonitor.open_db(self.working_db)
            good, total = mnet.pmonitor.get_status_count(db, self.stable_node())
            self.last_five_pings = mnet.pmonitor.get_last_five(db)
            db.close()
            return good, total
        return 0, 0
 
    def defaultIP(self) -> str:
        """
        Return the default interface
        """
        if self.node is not None and self.node.defaultIntf() is not None:
            return self.node.defaultIntf().ip
        return self.default_ip

    def stable_node(self) -> bool:
        """
        Indicates if the node is expected to always be reachable
        Default is True
        """
        return True


@dataclass
class IPPoolEntry:
    network: ipaddress.IPv4Network
    ip1: ipaddress.IPv4Interface
    ip2: ipaddress.IPv4Interface
    used: bool = False


@dataclass
class Uplink:
    sat_name: str
    distance: int
    ip_pool_entry: IPPoolEntry
    default: bool = False
    # [NEW] Store geometry for physics engine
    az: float = 0.0
    el: float = 90.0


class GroundStation(MNetNodeWrap):
    """
    State for a Ground Station

    Tracks established uplinks to satellites.
    Not a mininet node.
    """

    def __init__(self, name: str, default_ip: str, uplinks: list[dict[str,typing.Any]]) -> None:
        super().__init__(name, default_ip)
        self.uplinks: list[Uplink] = []
        self.ip_pool: list[IPPoolEntry] = []
        for link in uplinks:
            entry = IPPoolEntry(network=link["nw"], ip1=link["ip1"], ip2=link["ip2"])
            self.ip_pool.append(entry)

    def stable_node(self) -> bool:
        """
        Indicates that the node is not expected to be always reachable.
        """
        return False

    def has_uplink(self, sat_name: str) -> bool:
        # If logical record exists
        for u in self.uplinks:
            if u.sat_name == sat_name:
                return True

        # Also detect physical Mininet link
        try:
            n1 = self.frrt.net.getNodeByName(self.name)
            n2 = self.frrt.net.getNodeByName(sat_name)
            if self.frrt.net.linksBetween(n1, n2):
                return True
        except:
            pass

        return False


    def sat_links(self) -> list[str]:
        """
        Return a list of satellite names to which we have uplinks
        """
        return [uplink.sat_name for uplink in self.uplinks]

    def _get_pool_entry(self) -> IPPoolEntry | None:
        for entry in self.ip_pool:
            if not entry.used:
                entry.used = True
                return entry
        return None

    def add_uplink(self, sat_name: str, distance: int) -> Uplink | None:
        pool_entry = self._get_pool_entry()
        if pool_entry is None:
            return None
        uplink = Uplink(sat_name, distance, pool_entry)
        self.uplinks.append(uplink)
        return uplink

    def remove_uplink(self, sat_name: str) -> Uplink|None:
        for entry in self.uplinks:
            if entry.sat_name == sat_name:
                entry.ip_pool_entry.used = False
                self.uplinks.remove(entry)
                return entry
        return None


class FrrRouter(MNetNodeWrap):
    """
    Support an FRR router under mininet.
    - handles the the FRR config files, starting and stopping FRR.
    Does not cleanup config files.
    """

    CFG_DIR = "/etc/frr/{node}"
    VTY_DIR = "/var/frr/{node}/{daemon}.vty"
    LOG_DIR = "/var/log/frr/{node}"

    def __init__(self, name: str, default_ip: str):
        super().__init__(name, default_ip)
        self.no_frr = False
        self.vtysh = None
        self.daemons = None
        self.ospf = None
        #added for ground station uplinks.Stops those interval errors
        self.uplinks = []
        self.ip_pool = []

    # 🟢 Ground-station compatibility helpers
    def has_uplink(self, sat_name: str) -> bool:
        return any(uplink.sat_name == sat_name for uplink in self.uplinks)

    def add_uplink(self, sat_name: str, distance: int):
        entry = None
        for pool in self.ip_pool:
            if not pool.used:
                pool.used = True
                entry = pool
                break
        if entry is None:
            return None
        uplink = Uplink(sat_name, distance, entry)
        self.uplinks.append(uplink)
        return uplink

    def remove_uplink(self, sat_name: str):
        for uplink in list(self.uplinks):
            if uplink.sat_name == sat_name:
                uplink.ip_pool_entry.used = False
                self.uplinks.remove(uplink)
                return uplink
        return None

    def sat_links(self):
        return [uplink.sat_name for uplink in self.uplinks]


    def configure(self, vtysh: str, daemons: str, ospf: str) -> None:
        self.vtysh = vtysh
        self.daemons = daemons
        self.ospf = ospf

    def write_configs(self) -> None:
        # Get frr config and save to frr config directory
        cfg_dir = FrrRouter.CFG_DIR.format(node=self.name)
        log_dir = FrrRouter.LOG_DIR.format(node=self.name)

        # Suport this for running without mininet / FRR
        if self.no_frr:
            print("Warning: not running FRR")
            return

        uinfo = pwd.getpwnam("frr")

        if not os.path.exists(cfg_dir):
            # sudo install -m 775 -o frr -g frrvty -d {cfg_dir}
            print(f"create {cfg_dir}")
            os.makedirs(cfg_dir, mode=0o775)
            gid = grp.getgrnam("frrvty").gr_gid
            os.chown(cfg_dir, uinfo.pw_uid, gid)

        # sudo install -m 775 -o frr -g frr -d  {log_dir}
        if not os.path.exists(log_dir):
            print(f"create {log_dir}")
            os.makedirs(log_dir, mode=0o775)
            os.chown(log_dir, uinfo.pw_uid, uinfo.pw_gid)

        self.write_cfg_file(
            f"{cfg_dir}/vtysh.conf", self.vtysh, uinfo.pw_uid, uinfo.pw_gid
        )
        self.write_cfg_file(
            f"{cfg_dir}/daemons", self.daemons, uinfo.pw_uid, uinfo.pw_gid
        )
        self.write_cfg_file(
            f"{cfg_dir}/frr.conf", self.ospf, uinfo.pw_uid, uinfo.pw_gid
        )

    def start(self, net: mininet.net.Mininet) -> None:
        super().start(net)
        if self.node is None:
            self.no_frr = True
        self.write_configs()
        # Start frr daemons
        print(f"start router {self.name}")
        self.sendCmd(f"/usr/lib/frr/frrinit.sh start '{self.name}'")

    def stop(self):
        super().stop()
        # Cleanup and stop frr daemons
        print(f"stop router {self.name}")
        self.sendCmd(f"/usr/lib/frr/frrinit.sh stop '{self.name}'")

    def config_frr(self, daemon: str, commands: list[str]) -> bool:
        if self.node is None:
            # Running in stub mode
            return True

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = FrrRouter.VTY_DIR.format(node=self.name, daemon=daemon)
        result = True
        try:
            sock.connect(path)
            msg = b'enable\x00'
            result = result and self._send_frr_cmd(sock, msg)
            msg = b'conf term file-lock\x00'
            result = result and self._send_frr_cmd(sock, msg)
            for command in commands:
                print(f"sending command {command} to {self.name}")
                msg = (command + '\x00').encode("ascii")
                result = result and self._send_frr_cmd(sock, msg)
            msg = b'end\x00'
            self._send_frr_cmd(sock, msg)
            msg = b'disable\x00'
            self._send_frr_cmd(sock, msg)
        except TimeoutError:
            print("timout connecting to FRR")
            result = False
        sock.close()
        return result

    def _send_frr_cmd(self, sock, msg: bytes) -> bool:
        sock.sendall(msg)
        data = sock.recv(10000)
        size = len(data)
        if size > 0 and data[size-1] == 0:
            return True
        return False

    def write_cfg_file(self, file_path: str, contents: str, uid: int, gid: int) -> None:
        if self.no_frr:
            return

        print(f"write {file_path}")
        with open(file_path, "w") as f:
            f.write(contents)
            f.close()
        os.chmod(file_path, 0o640)
        os.chown(file_path, uid, gid)



class StubMininet:
    """
    In order to run and test with out standing up an entire mininet environment (that is run as root),
    we can stub out the mininet calls. This results in the mininet nodes being returned as None and code
    needs to handle this case.
    """
    def __init__(self):
        pass

    def configLinkStatus(self, node1: str, node2: str, state: str):
        pass

    def linksBetween(self, node1, node2):
        return []

    def getNodeByName(self, name):
        return None
    
    def addLink(self, node1: str, node2: str, params1: dict, params2: dict):
        pass
    
    def delLinkBetween(self, node1, node2):
        pass


class NetxTopo(mininet.topo.Topo):
    """
    Mininet topology object used to build the virtual network.
    """
    def __init__(self, graph: networkx.Graph):
        self.graph = graph
        self.routers: list[FrrRouter] = []
        self.ground_stations: list[GroundStation] = []
        super().__init__()

    def build(self, *args, **params):
        """
        Build the network according to the information in the networkx.Graph
        """
        # Create routers
        for name in torus_topo.satellites(self.graph):
            node = self.graph.nodes[name]
            ip = node.get("ip")
            ip_intf = None
            ip_addr = None
            if ip is not None:
                ip_intf = format(ip)
                ip_addr = format(ip.ip)
            self.addHost(
                name,
                cls=RouteNode,
                ip=ip_intf)

            frr_router: FrrRouter = FrrRouter(name, ip_addr) 
            self.routers.append(frr_router)
            frr_router.configure(
                ospf=node["ospf"],
                vtysh=node["vtysh"],
                daemons=node["daemons"]
            )

        for name in torus_topo.ground_stations(self.graph):
            node = self.graph.nodes[name]
            ip = node.get("ip")
            ip_intf = None
            ip_addr = None
            if ip is not None:
                ip_intf = format(ip)
                ip_addr = format(ip.ip)

            # 🟢 Create as FRR router instead of plain host
            self.addHost(name, cls=RouteNode, ip=ip_intf)
            frr_station = FrrRouter(name, ip_addr)

            # Load FRR configs for the ground station, reuse OSPF and daemons templates
            # from satellite nodes or generate minimal ones.
            frr_station.configure(
                vtysh=f"""
                service integrated-vtysh-config
                hostname {name}
                log file /var/log/frr/{name}/frr.log
                """,
                        daemons="""zebra=yes
                ospfd=yes
                staticd=yes
                mgmtd=yes
                """,
                ospf=f"""
                ! 1. Define the Filter (Prefix List)
                ! Deny the private LAN range (192.168.0.0/16)
                ip prefix-list PL_FILTER_PRIVATE seq 5 deny 192.168.0.0/16 le 32
                ! Permit everything else (Public IPs, Uplinks, Loopbacks)
                ip prefix-list PL_FILTER_PRIVATE seq 10 permit 0.0.0.0/0 le 32

                ! 2. Define the Policy (Route Map)
                route-map RM_OSPF_FILTER permit 10
                match ip address prefix-list PL_FILTER_PRIVATE

                ! 3. Apply the Policy to OSPF
                router ospf
                ! Apply the filter to all redistribution types
                redistribute connected route-map RM_OSPF_FILTER
                redistribute kernel route-map RM_OSPF_FILTER
                redistribute static route-map RM_OSPF_FILTER
                
                ! Only form OSPF neighbors on the Satellite Uplinks (Backbone)
                network 10.250.0.0/16 area 0.0.0.0
                
                passive-interface default
                ! allow uplink OSPF adjacency
                no passive-interface eth0
                """
            )

            # Register in both router and ground station lists
            self.routers.append(frr_station)
            self.ground_stations.append(frr_station)

        # -------------------------------------------------------
        # IoT edge per Ground Station:
        # iot_* -> gw_* (Linux node)
        # and inside gw_* we will create an OVS bridge br-iot-*
        # -------------------------------------------------------
        # [NEW CODE]
        for gs_name in torus_topo.ground_stations(self.graph):
            gs_code = self._gs_code(gs_name)
            suffix = gs_name.replace("G_", "").lower()
            gw = f"gw_{suffix}"     # The L3 Router (Gateway)
            sw = f"sw_{suffix}"     # The L2 Switch (OVS)

            # 1. Create the Gateway Node (L3)
            self.addHost(gw, cls=RouteNode)

            # 2. Create the Switch Node (L2)
            # This automatically handles the OVS bridge creation in the correct namespace
            self.addSwitch(sw, cls=OVSKernelSwitch, protocols='OpenFlow13', dpid=f"{gs_code:016x}")

            # 3. Link Gateway -> Ground Station (Uplink)
            self.addLink(gw, gs_name, intfName1=f"{gw}-upl", intfName2=f"{gs_name}-gw", bw=20, max_queue_size=1000, delay="1ms")

            # 4. Link Gateway -> Switch (The "Router-on-a-stick" link)
            # This creates the physical link between L3 and L2
            self.addLink(gw, sw, intfName1=f"{gw}-lan", cls=TCLink, bw=1000, max_queue_size=50, delay="1ms")

            # 5. Link IoT Hosts -> Switch
            for i in range(4):
                h = f"iot_{suffix}_{i}"
                self.addHost(h)
                # Connect IoT devices to the Switch, NOT the Gateway
                self.addLink(h, sw, cls=TCLink, bw=10, max_queue_size=50, delay="1ms")


        # Create links between routers
        for name, edge in self.graph.edges.items():
            router1 = name[0]
            router2 = name[1]

            # Handle incomplete edged
            if edge.get("ip") is None:
                self.addLink(router1, router2)
                return

            ip1 = edge["ip"][router1]
            intf1 = edge["intf"][router1]

            ip2 = edge["ip"][router2]
            intf2 = edge["intf"][router2]

            self.addLink(
                router1,
                router2,
                intfName1=intf1,
                intfName2=intf2,
                params1={"ip": format(ip1)},
                params2={"ip": format(ip2)},
                cls=TCLink,
                bw=100,
                delay="10ms",
                max_queue_size=1000
            )

    def _gs_code(self, station_name: str) -> int:
        mapping = {
            "G_PAO": 10,
            "G_HND": 20,
            "G_ZRH": 30,
            "G_SYD": 40,
        }
        if station_name not in mapping:
            names = sorted(torus_topo.ground_stations(self.graph))
            mapping[station_name] = 50 + 10 * names.index(station_name)
        return mapping[station_name]


class FrrSimRuntime:
    """
    Code for the FRR / Mininet / Monitoring functions.
    """
    def __init__(self, topo: NetxTopo, net: mininet.net.Mininet, stable_monitor: bool =False):
        self.graph = topo.graph

        self.last_uplink_change: dict[str, tuple[str | None, float]] = {} # including this for dwell time
        self.nodes: dict[str, MNetNodeWrap] = {}
        self.routers: dict[str, FrrRouter] = {}
        self.ground_stations: dict[str, GroundStation] = {}
        self.stable_monitor = stable_monitor
        self._pending_iot_ospf_ads: list[tuple[str, str]] = []  # (gs_name, pub_net)

        # Create monitoring DB file.
        fd, self.db_file = tempfile.mkstemp(suffix=".sqlite")
        open(fd, "r").close()
        print(f"Master db file {self.db_file}")

        for frr_router in topo.routers:
            self.nodes[frr_router.name] = frr_router
            self.routers[frr_router.name] = frr_router
        for ground_station in topo.ground_stations:
            self.nodes[ground_station.name] = ground_station
            self.ground_stations[ground_station.name] = ground_station

        self.stat_samples = []
        self.net = net
        self.stub_net = False
        # ---------------------------------------------------
        # GLOBAL UNIQUE /30 IP POOL FOR ALL GS–SAT LINKS
        # ---------------------------------------------------
        self._ip_pool = []
        self._ip_pool_index = 0

        base_network = ipaddress.ip_network("10.250.0.0/16")
        # Precompute ALL /30 networks inside 10.250.0.0/16
        for subnet in base_network.subnets(new_prefix=30):
            self._ip_pool.append(subnet)


        # If net is none, we are running in a stub mode without mininet or FRR.
        if self.net is None:
            self.net = StubMininet()
            self.stub_net = True
        
        # -----------------------------
        # Configure IoT edge per GS
        # -----------------------------
        if not self.stub_net:
            for gs_name in self.ground_stations.keys():
                suffix = gs_name.replace("G_", "").lower()
                gw_name = f"gw_{suffix}"
                if gs_name in self.net.nameToNode and gw_name in self.net.nameToNode:
                    self._config_iot_for_station(gs_name)
                else:
                    print(f"[IOT][WARN] missing nodes for {gs_name}: {gs_name in self.net.nameToNode=} {gw_name in self.net.nameToNode=}")
        
        self.rain_model = RainModel()
        self.storms = self.rain_model.load_config("rain_cells.json")



    def start_routers(self) -> None: 
        # Populate master db file
        data = []
        seen_ips = set()

        for router in self.routers.values():
            ip = router.defaultIP()
            if ip not in seen_ips:
                data.append((router.name, ip, router.stable_node()))
                seen_ips.add(ip)

        for station in self.ground_stations.values():
            ip = station.defaultIP()
            if ip not in seen_ips:
                data.append((station.name, ip, station.stable_node()))
                seen_ips.add(ip)

        mnet.pmonitor.init_targets(self.db_file, data)

        # Start all nodes
        for node in self.nodes.values():
            node.start(self.net)

        # Wait for start to complete.
        for node in self.nodes.values():
            node.waitOutput()
        
        if not self.stub_net:
            for gs_name in self.ground_stations.keys():
                self._config_iot_for_station(gs_name)

        # Start monitoring on all nodes
        db_master = mnet.pmonitor.open_db(self.db_file)
        for node in self.nodes.values():
            # Start monitor if node is not considered always reachable
            # or we are running monitoring from the stable nodes.
            if self.stable_monitor or not node.stable_node():
                node.startMonitor(self.db_file, db_master)
        db_master.close()

        # Wait for monitoring to start
        for node in self.nodes.values():
            if self.stable_monitor or not node.stable_node():
                node.waitOutput()

        for gs_name, pub_net in getattr(self, "_pending_iot_ospf_ads", []):
            if gs_name not in self.routers:
                continue
            self.routers[gs_name].config_frr(
                "ospfd",
                [
                    "router ospf",
                    f" network {pub_net} area 0.0.0.0",
                ],
            )
        
        self._pending_iot_ospf_ads.clear()



    def stop_routers(self):
        # Stop monitor on all nodes
        db_master = mnet.pmonitor.open_db(self.db_file)
        for node in self.nodes.values():
            node.stopMonitor(db_master)
        db_master.close()

        for node in self.nodes.values():
            node.stop()

        # Wait for commands to complete - important!.
        # Otherwise processes may not shut down.
        for node in self.nodes.values():
            node.waitOutput()
        os.unlink(self.db_file)

    def update_monitor_stats(self):
        stable_good_count: int = 0
        stable_total_count: int = 0
        dynamic_good_count: int = 0
        dynamic_total_count: int = 0

        if self.stub_net:
            stable_good_count: int = random.randrange(20)
            stable_total_count: int = random.randrange(20) + stable_good_count
            dynamic_good_count: int = random.randrange(20)
            dynamic_total_count: int = random.randrange(20) + dynamic_good_count
        else:
            for node in self.nodes.values():
                good, total = node.update_monitor_stats()
                if node.stable_node():
                    stable_good_count += good
                    stable_total_count += total
                else:
                    dynamic_good_count += good
                    dynamic_total_count += total

        self.stat_samples.append((datetime.datetime.now(), 
                                    stable_good_count, stable_total_count,
                                    dynamic_good_count, dynamic_total_count))
        if len(self.stat_samples) > 200:
            self.stat_samples.pop(0)

    def get_last_five_stats(self) -> dict[str, list[tuple[str,bool]]]:
        result: dict[str, list[tuple[str,bool]]] = {}
        for node in self.nodes.values():
            result[node.name] = node.last_five_pings
        return result

    def sample_stats(self):
        self.update_monitor_stats()

    def get_node_status_list(self, name: str):
        node = self.nodes[name]
        result = []
        if not self.stub_net and os.path.getsize(node.working_db) > 0:
            db_working = mnet.pmonitor.open_db(node.working_db)
            result = mnet.pmonitor.get_status_list(db_working)
            db_working.close()
        return result

    def apply_weather(self):
        """
        Calculates rain fade for all active uplinks (Per Link).
        """
        for gs_name, station in self.ground_stations.items():
            if not station.uplinks:
                continue
            
            lat = getattr(station, 'lat', 0.0)
            lon = getattr(station, 'lon', 0.0)
            
            # Iterate over EVERY active uplink for this station
            for uplink in station.uplinks:
                sat_name = uplink.sat_name
                
                # Retrieve the Az/El we stored in the Uplink object
                az = getattr(uplink, 'az', 0.0)
                el = getattr(uplink, 'el', 90.0)

                # Calculate Physics
                total_loss_db = 0.0
                for storm in self.storms:
                    loss = self.rain_model.calculate_path_loss(lat, lon, az, el, storm)
                    total_loss_db += loss
                
                # Map dB to Mininet Config
                applied_loss = max(0.0, min(100.0, 1.0 + total_loss_db))
                
                # Bandwidth Collapse (15% reduction per dB > 0)
                base_bw = 10.0
                if total_loss_db > 0:
                    reduction = max(0.01, 1.0 - (total_loss_db * 0.15))
                    applied_bw = base_bw * reduction
                else:
                    applied_bw = base_bw

                # Apply to Mininet Link
                try:
                    gs_node = self.net.getNodeByName(gs_name)
                    sat_node = self.net.getNodeByName(sat_name)
                    links = self.net.linksBetween(gs_node, sat_node)
                    
                    if links:
                        # Apply to both sides of the link
                        links[0].intf1.config(bw=applied_bw, loss=applied_loss) 
                        links[0].intf2.config(bw=applied_bw, loss=applied_loss)
                except Exception as e:
                    print(f"[WEATHER] Error updating {gs_name}->{sat_name}: {e}")

    def get_stat_samples(self):
        return self.stat_samples

    def get_topo_graph(self) -> networkx.Graph:
        return self.graph

    def get_ring_list(self) -> list[list[str]]:
        return self.graph.graph["ring_list"]

    def get_router_list(self) -> list[tuple[str,str]]:
        result = []
        for name in torus_topo.satellites(self.graph):
            node = self.graph.nodes[name]
            ip = ""
            if node.get("ip") is not None:
                ip = format(node.get("ip"))
            else:
                ip = ""
            result.append((name, ip))
        return result

    def get_link_list(self) -> list[tuple[str,str,str]]:
        result = []
        for edge in self.graph.edges:
            node1 = edge[0]
            node2 = edge[1]
            ip_str = []
            for ip in self.graph.edges[node1, node2]["ip"].values():
                ip_str.append(format(ip))
            result.append((node1, node2, "-".join(ip_str)))
        return result

    def get_link(self, node1: str, node2: str):
        if self.graph.nodes.get(node1) is None:
            return f"{node1} does not exist"
        if self.graph.nodes.get(node2) is None:
            return f"{node2} does not exist"
        edge = self.graph.adj[node1].get(node2)
        if edge is None:
            return f"link {node1}-{node2} does not exist"
        return (node1, node2, edge["ip"][node1], edge["ip"][node2])

    def get_router(self, name: str):
        if self.graph.nodes.get(name) is None:
            return f"{name} does not exist"
        result = {"name": name, "ip": self.graph.nodes[name].get("ip"), "neighbors": {}}
        for neighbor in self.graph.adj[name].keys():
            edge = self.graph.adj[name][neighbor]
            result["neighbors"][neighbor] = {
                "ip_local": edge["ip"][name],
                "ip_remote": edge["ip"][neighbor],
                "up": self.get_link_state(name, neighbor),
                "intf_local": edge["intf"][name],
                "intf_remote": edge["intf"][neighbor],
            }
        return result
    
    def _gs_code(self, station_name: str) -> int:
        """
        Stable numeric code per GS for addressing.
        Customize this mapping to match your GS names.
        """
        mapping = {
            "G_PAO": 10,
            "G_HND": 20,
            "G_ZRH": 30,
            "G_SYD": 40,
        }
        # fallback: assign based on sorted order if unknown
        if station_name not in mapping:
            names = sorted(self.ground_stations.keys())
            mapping[station_name] = 50 + 10 * names.index(station_name)
        return mapping[station_name]
    
    #_add_iot_for_station method deleted because adding IoT + gateway nodes in the topology build phase.


    def _config_iot_for_station(self, station_name: str):
            gs_code = self._gs_code(station_name)
            suffix = station_name.replace("G_", "").lower()
            gw_name = f"gw_{suffix}"
            
            # Interface on the Gateway that connects to the new OVS Switch
            gw_lan_intf = f"{gw_name}-lan" 

            # IP Configuration
            lan_net = f"192.168.{gs_code}.0/24"
            gw_lan_ip = f"192.168.{gs_code}.1/24"
            
            # Uplink IPs (Gateway <-> Ground Station)
            p2p_gw  = f"172.16.{gs_code}.1/30"
            p2p_gs  = f"172.16.{gs_code}.2/30"
            p2p_gw_ip = f"172.16.{gs_code}.1"
            p2p_gs_ip = f"172.16.{gs_code}.2"
            pub_net = f"100.64.{gs_code}.0/24"

            gs = self.net.getNodeByName(station_name)
            gw = self.net.getNodeByName(gw_name)
            
            # 1. Configure Uplink (GW <-> GS)
            gw_upl_if = f"{gw_name}-upl"
            gs_if = f"{station_name}-gw"
            
            gw.cmd(f"ip link set {gw_upl_if} up")
            gw.cmd(f"ip addr flush dev {gw_upl_if}")
            gw.cmd(f"ip addr add {p2p_gw} dev {gw_upl_if}")

            gs.cmd(f"ip link set {gs_if} up")
            gs.cmd(f"ip addr flush dev {gs_if}")
            gs.cmd(f"ip addr add {p2p_gs} dev {gs_if}")
            
            # 2. Configure LAN Interface (GW <-> Switch)
            # This gives the Gateway its "Router" IP (192.168.x.1)
            gw.cmd(f"ip link set {gw_lan_intf} up")
            gw.cmd(f"ip addr flush dev {gw_lan_intf}")
            gw.cmd(f"ip addr add {gw_lan_ip} dev {gw_lan_intf}")

            # 3. Configure IoT Hosts (The part that was failing!)
            # This overwrites the default 10.x.x.x IP with the correct 192.168.x.x IP
            iot_hosts = [self.net.getNodeByName(f"iot_{suffix}_{i}") for i in range(4)]
            for i, host in enumerate(iot_hosts):
                host_if = host.defaultIntf().name
                host.cmd(f"ip addr flush dev {host_if}")
                
                # Assign new IP
                host_ip = f"192.168.{gs_code}.{10+i}/24"
                host.cmd(f"ip addr add {host_ip} dev {host_if}")
                
                # Set Default Route to Gateway
                host.cmd(f"ip route replace default via 192.168.{gs_code}.1")

            # 4. Routing & NAT
            gw.cmd(f"ip route replace default via {p2p_gs_ip}")
            gs.cmd(f"ip route replace {pub_net} via {p2p_gw_ip}")
            gs.cmd(f"ip route replace {lan_net} via {p2p_gw_ip}")

            gw.cmd("sysctl -w net.ipv4.ip_forward=1")
            gw.cmd("iptables -t nat -F")
            gw.cmd(f"iptables -t nat -A POSTROUTING -s {lan_net} -o {gw_upl_if} -j NETMAP --to {pub_net}")
            gw.cmd(f"iptables -t nat -A PREROUTING -i {gw_upl_if} -d {pub_net} -j NETMAP --to {lan_net}")

            # 5. OSPF Advertising & Routing Fix
            # We REMOVE the IP from the dummy interface so the kernel forwards traffic
            # instead of trying to consume it.

            # Ensure the dummy interface exists (we might need it for OSPF network commands to attach to)
            gs.cmd("ip link add iotpub0 type dummy 2>/dev/null || true")
            gs.cmd("ip link set dev iotpub0 up")

            # CRITICAL CHANGE: Do NOT add the IP address to the interface. 
            # Instead, add a STATIC ROUTE pointing to the Gateway.
            # This tells GS: "To find 100.64.10.x, go to 172.16.10.1 (The Gateway)"
            gs.cmd(f"ip route add {pub_net} via {p2p_gw_ip}")



            gs.cmd(f"ip route replace {lan_net} via {p2p_gw_ip}")

            # [FIX 1] Enable Forwarding on the Ground Station so it acts as a router
            gs.cmd("sysctl -w net.ipv4.ip_forward=1")
            
            # [FIX 2] Enable Forwarding on the Gateway (just to be safe)
            gw.cmd("sysctl -w net.ipv4.ip_forward=1")


            if (station_name, pub_net) not in self._pending_iot_ospf_ads:
                self._pending_iot_ospf_ads.append((station_name, pub_net))
                
            print(f"[IOT] Configured {station_name}: IoT IPs 192.168.{gs_code}.10-13 -> GW 192.168.{gs_code}.1")

    def get_ground_stations(self) -> list[GroundStation]:
        return [x for x in self.ground_stations.values()]

    def get_station(self, name):
        return self.ground_stations[name]

    def set_link_state(
        self, node1: str, node2: str, state_up: bool):
        if self.graph.nodes.get(node1) is None:
            return f"{node1} does not exist"
        if self.graph.nodes.get(node2) is None:
            return f"{node2} does not exist"
        adj = self.graph.adj[node1].get(node2)
        if self.graph.adj[node1].get(node2) is None:
            return f"{node1} to {node2} does not exist"
        self._config_link_state(node1, node2, state_up)
        return None

    def _config_link_state(
        self, node1: str, node2: str, state_up: bool 
    ):
        state = "up" if state_up else "down"
        self.net.configLinkStatus(node1, node2, state)

    def get_link_state(self, node1: str, node2: str) -> tuple[bool, bool]:
        n1 = self.net.getNodeByName(node1)
        n2 = self.net.getNodeByName(node2)
        links = self.net.linksBetween(n1, n2)
        if len(links) > 0:
            link = links[0]
            return link.intf1.isUp(), link.intf2.isUp()

        return False, False

    def set_station_uplinks(self, station_name: str, uplinks: simapi.UpLinks) -> bool:
        """
        Update the uplinks for a given ground station (Supports Dual Uplinks).
        """
        if station_name not in self.ground_stations:
            print(f"[ERROR] Unknown ground station {station_name}")
            return False

        station = self.ground_stations[station_name]
        station.lat = getattr(uplinks, 'gs_lat', 0.0)
        station.lon = getattr(uplinks, 'gs_lon', 0.0)

        # 1. Map requested satellites to their new data
        # Dict format: { 'SAT_NAME': (distance, az, el) }
        desired_sats = {
            u.sat_node: (u.distance, u.az_deg, u.el_deg) 
            for u in uplinks.uplinks
        }
        
        # 2. Cleanup Old Links
        # Remove any link that is currently active but NOT in the new desired list
        for old_uplink in list(station.uplinks):
            if old_uplink.sat_name not in desired_sats:
                print(f"[CLEANUP] Removing {station_name} -> {old_uplink.sat_name}")
                self._remove_link(
                    station_name,
                    old_uplink.sat_name,
                    old_uplink.ip_pool_entry.network,
                    old_uplink.ip_pool_entry.ip1,
                )
                old_uplink.ip_pool_entry.used = False
                station.uplinks.remove(old_uplink)

        # 3. Create or Update Links
        for sat_name, (dist, az, el) in desired_sats.items():
            
            # Check if we already have this link
            existing_uplink = next((u for u in station.uplinks if u.sat_name == sat_name), None)
            
            if existing_uplink:
                # [UPDATE] Just update the physics data!
                existing_uplink.distance = dist
                existing_uplink.az = az
                existing_uplink.el = el
            else:
                # [CREATE] New Link
                print(f"[NEW] Creating uplink {station_name} -> {sat_name}")
                
                has_free_ip = any(not entry.used for entry in station.ip_pool)
                
                if not has_free_ip:
                     # Allocate a new /30 subnet from the global pool
                     if self._ip_pool_index < len(self._ip_pool):
                         subnet = self._ip_pool[self._ip_pool_index]
                         self._ip_pool_index += 1
                         ip1, ip2 = list(subnet.hosts())
                         # Create the entry
                         entry = IPPoolEntry(
                             subnet, 
                             ipaddress.IPv4Interface(f"{ip1}/30"), 
                             ipaddress.IPv4Interface(f"{ip2}/30")
                         )
                         station.ip_pool.append(entry)
                         print(f"[IP] Allocated new subnet {subnet} to {station_name}")
                     else:
                         print(f"[ERROR] No more IPs available in global pool for {station_name}!")
                # [FIX END]

                uplink_obj = station.add_uplink(sat_name, dist)
                if uplink_obj:
                    # Initialize physics data
                    uplink_obj.az = az
                    uplink_obj.el = el
                    
                    ip_entry = uplink_obj.ip_pool_entry
                    
                    # Create Physical Link
                    self._create_uplink(station_name, sat_name, ip_entry.network, ip_entry.ip1, ip_entry.ip2)

                    # Add Static Route on Satellite (Back to GS)
                    try:
                        n1 = self.net.getNodeByName(station_name)
                        loopback_ip = n1.IP(intf="loop")
                        sat_ip = str(ip_entry.ip1.ip) 
                        cmd = f"ip route add {loopback_ip}/32 via {sat_ip}"
                        self.routers[sat_name].config_frr("staticd", [cmd])
                    except Exception as e:
                        print(f"[ERROR] static route failed: {e}")

        # 4. Update Default Route (ECMP)
        self._update_default_route_ecmp(station)
        
        # 5. Apply Weather Immediately
        self.apply_weather()

        return True
    
    def _update_default_route_ecmp(self, station: GroundStation) -> None:
        """
        Updates the default route to use ECMP across all active uplinks.
        """
        station_node = self.net.getNodeByName(station.name)
        if not station_node: return

        if not station.uplinks:
            station_node.cmd("ip route del default")
            return

        # Build ECMP Route
        # "nexthop via 10.250.0.2 weight 1 nexthop via 10.250.0.6 weight 1"
        nexthops = []
        for uplink in station.uplinks:
            gw_ip = format(uplink.ip_pool_entry.ip2.ip) # Satellite Side IP
            nexthops.append(f"nexthop via {gw_ip} weight 1")
        
        cmd = "ip route replace default " + " ".join(nexthops)
        station_node.cmd(cmd)
    
    
    def _create_uplink(
        self,
        station_name: str,
        sat_name: str,
        ip_nw: ipaddress.IPv4Network,
        ip1: ipaddress.IPv4Interface,
        ip2: ipaddress.IPv4Interface,
    ):
        # Create the link
        self.net.addLink(
            station_name, sat_name,
            cls=TCLink,
            params1={"ip": format(ip1), "delay": "20ms"},
            params2={"ip": format(ip2), "delay": "20ms"},
            jitter="5ms",
            loss=1,
            bw=10,
            max_queue_size=100
        )

        # ----------------------------------------------------------
        # Find interface names created by Mininet (GS and Satellite)
        # ----------------------------------------------------------
        station_node = self.net.getNodeByName(station_name)
        sat_node = self.net.getNodeByName(sat_name)

        link_info = self.net.linksBetween(station_node, sat_node)[0]

        # Interface on station side
        if station_name in link_info.intf1.name:
            station_iface = link_info.intf1.name
            sat_iface = link_info.intf2.name
        else:
            station_iface = link_info.intf2.name
            sat_iface = link_info.intf1.name

        # ----------------------------------------------------------
        # Satellite OSPF configuration
        # ----------------------------------------------------------
        sat_router = self.routers[sat_name]

        # Enable the network
        sat_router.config_frr(
            "ospfd",
            [
                "router ospf",
                " network 10.250.0.0/16 area 0.0.0.0"
            ]
        )

        # Activate interface for OSPF
        sat_router.config_frr(
            "ospfd",
            [
                f"interface {sat_iface}",
                " no ip ospf passive",
            ]
        )

        # ----------------------------------------------------------
        # Ground Station OSPF configuration
        # ----------------------------------------------------------
        gs_router = self.routers[station_name]

        gs_router.config_frr(
            "ospfd",
            [
                f"interface {station_iface}",
                " no ip ospf passive"
            ]
        )

        gs_router.config_frr(
            "ospfd",
            [
                "router ospf",
                " network 10.250.0.0/16 area 0.0.0.0"
            ]
        )

        # Configure FRR daemons to handle the uplink
        station = self.ground_stations[station_name]
        frr_router = self.routers[sat_name]

        # Set a static route on the satellite node that refers to the ground station loopback IP
        # ip route {ground station ip /32} {ground station pool ip}
        station_lo = ipaddress.IPv4Interface(station.defaultIP()).ip
        frr_router.config_frr("staticd", [f"ip route {station_lo}/32 {ip1.ip}"])

        # After GS↔SAT uplink is created and IPs exist, refresh IoT NAT for this GS
        try:
            self._config_iot_for_station(station_name)
        except Exception as e:
            print(f"[IOT][WARN] NAT refresh failed for {station_name}: {e}")


    def _remove_link(
        self,
        station_name: str,
        sat_name: str,
        ip_nw: ipaddress.IPv4Network,
        ip: ipaddress.IPv4Interface,
    ) -> None:
        """
        Safely tear down a single GS↔SAT uplink.

        - Removes the static route on the SAT back to the GS loopback
        - Deletes the Mininet link only if it still exists
        - Flushes the GS-side interface addresses for that /30
        - Never crashes if the link is already gone
        """

        station_node = self.net.getNodeByName(station_name)
        sat_node = self.net.getNodeByName(sat_name)

        print(f"\n[REMOVE] Request to remove uplink {station_name} ↔ {sat_name}")
        print(f"[REMOVE] Expected subnet: {ip_nw}, GS-side IP: {ip}")

        # ------------------------------------------------------------------
        # 1) Remove static route on the SAT router
        # ------------------------------------------------------------------
        try:
            station = self.ground_stations[station_name]
            frr_router = self.routers[sat_name]
            cmd = f"no ip route {station.defaultIP()}/32 {ip.ip}"
            print(f"[REMOVE] Removing static route on {sat_name}: {cmd}")
            frr_router.config_frr("staticd", [cmd])
        except Exception as e:
            print(f"[REMOVE][WARN] Failed to remove static route: {e}")

        # ------------------------------------------------------------------
        # 2) Delete Mininet link ONLY if it exists
        # ------------------------------------------------------------------
        try:
            existing = self.net.linksBetween(station_node, sat_node)
            print(f"[REMOVE] linksBetween({station_name},{sat_name}) = {len(existing)}")

            if existing:
                # There should be at most one GS↔SAT uplink now, but even if
                # there are multiple, delLinkBetween() will just remove one.
                print(f"[REMOVE] Deleting Mininet link {station_name} ↔ {sat_name}")
                self.net.delLinkBetween(station_node, sat_node)
            else:
                print("[REMOVE] No Mininet links to delete (already gone).")
        except Exception as e:
            print(f"[REMOVE][ERROR] delLinkBetween failed: {e}")

        # ------------------------------------------------------------------
        # 3) Flush GS interface that carries this /30 subnet
        # ------------------------------------------------------------------
        try:
            for intf in station_node.intfs.values():
                if intf.name == "lo":
                    continue

                out = station_node.cmd(f"ip -o -4 addr show dev {intf.name}")
                for line in out.splitlines():
                    parts = line.split()
                    if "inet" not in parts:
                        continue
                    idx = parts.index("inet")
                    if idx + 1 >= len(parts):
                        continue

                    addr = parts[idx + 1]  # e.g., "10.250.0.1/30"
                    try:
                        iface_ip = ipaddress.IPv4Interface(addr)
                    except Exception:
                        continue

                    if iface_ip.network == ip_nw:
                        print(f"[REMOVE] Flushing IPs on {intf.name} ({iface_ip})")
                        station_node.cmd(f"ip addr flush dev {intf.name}")
                        break
        except Exception as e:
            print(f"[REMOVE][WARN] Failed to flush GS interface IPs: {e}")

        print(f"[REMOVE] Finished cleanup for {station_name} ↔ {sat_name}\n")

    def _update_default_route(self, station: GroundStation) -> None:
        closest_uplink = None
        # Find closest uplink
        for uplink in station.uplinks:
            if closest_uplink is None:
                closest_uplink = uplink
            elif uplink.distance < closest_uplink.distance:
                closest_uplink = uplink

        
        # If the closest has changed, update the default route
        if closest_uplink is not None and not closest_uplink.default:
            # Clear current default
            for uplink in station.uplinks:
                uplink.default = False
            # Mark new default and set
            closest_uplink.default = True 
            station_node = self.net.getNodeByName(station.name)
            route = "via %s" % format(closest_uplink.ip_pool_entry.ip2.ip)
            print(f"set default route for {station.name} to {route}")
            if station_node is not None:
                station_node.setDefaultRoute(route)
 
