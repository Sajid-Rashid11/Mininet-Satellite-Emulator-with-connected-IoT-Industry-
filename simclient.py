"""
Client to drive the JSON api implemented in driver.py
"""
import requests
import simapi

class Client:
    def __init__(self, url: str) -> None:
        self.url = url

    def set_link_state(self, node1: str, node2: str, up: bool) -> None:
        try:
            print(f"send link state {node1}, {node2}, state up {up}")
            data = simapi.Link(node1_name=node1, node2_name=node2, up="true" if up else "false")
            url = f"{self.url}/link"
            r = requests.put(url, 
                    json=data.model_dump())
            print(r.text)
        except requests.exceptions.ConnectionError as e:
            print(e)
            pass

    def set_uplinks(self, ground_node: str, gs_lat: float, gs_lon: float, uplinks: list[dict]) -> None:
        """
        Send uplink updates to the simulation driver.
        
        Args:
            ground_node: Name of the ground station (e.g., "G_SYD")
            gs_lat: Ground Station Latitude (for rain model)
            gs_lon: Ground Station Longitude
            links: List of dicts containing:
                   {'sat_node': str, 'distance': int, 'az_deg': float, 'el_deg': float}
        """
        try:
            # Create the Pydantic model with the new fields
            data = simapi.UpLinks(
                ground_node=ground_node,
                gs_lat=gs_lat,  # [NEW]
                gs_lon=gs_lon,  # [NEW]
                uplinks=[]
            )

            # Iterate through the list of link dictionaries
            for link in uplinks:
                data.uplinks.append(simapi.UpLink(
                    sat_node=link['sat_node'],
                    distance=link['distance'],
                    az_deg=link['az_deg'],  # [NEW]
                    el_deg=link['el_deg']   # [NEW]
                ))

            url = f"{self.url}/uplinks"
            
            # Use PUT (as defined in driver.py)
            r = requests.put(url, json=data.model_dump())
            
            # Optional: Only print if error to reduce console spam
            if r.status_code != 200:
                print(f"[Client] Error sending uplinks: {r.text}")

        except requests.exceptions.ConnectionError as e:
            print(f"[Client] Connection Error: {e}")
        except Exception as e:
            print(f"[Client] set_uplinks failed: {e}")


    def get_links(self, node_name):
        """
        Query the Mininet REST API for active links involving this node.
        Returns a list of dicts: [{'node1':..., 'node2':..., 'intf1':..., 'intf2':...}, ...]
        """
        try:
            resp = requests.get(f"{self.url}/links/{node_name}")
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"[WARN] Failed to get links for {node_name}: HTTP {resp.status_code}")
                return []
        except Exception as e:
            print(f"[ERROR] get_links({node_name}): {e}")
            return []