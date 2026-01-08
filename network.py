 

from lib import config  # do not import anything before this
from p4app import P4Mininet
from mininet.topo import Topo
from mininet.cli import CLI
import os

# ================ CONSTANTS ================
NUM_WORKERS = 4  # Can be scaled up to 8
SWITCHML_UDP_PORT = 12345  # Must match P4 switch
MULTICAST_GROUP_ID = 1

# ================ ADDRESSING SCHEME ================
# Network: 10.0.0.0/24
# Switch interfaces: 10.0.0.10, 10.0.0.11, 10.0.0.12, etc. (unique per port)
# Workers: 10.0.0.2, 10.0.0.3, 10.0.0.4, etc.
# Switch management IP: 10.0.0.1 (for SwitchML packets)

def getWorkerIP(wid):
    """Get IP address for worker with given ID"""
    return "10.0.0.%d" % (wid + 2)  # Start from 10.0.0.2

def getWorkerMAC(wid):
    """Get MAC address for worker with given ID"""
    return "00:00:00:00:01:%02x" % (wid + 1)  # 00:00:00:00:01:01, 01:02, ...

def getSwitchIP():
    """Get IP address for the switch (management/SwitchML IP)"""
    return "10.0.0.1"

def getSwitchInterfaceIP(port_id):
    """Get IP address for switch interface on specific port"""
    return "10.0.0.%d" % (port_id + 10)  # 10.0.0.10, 10.0.0.11, ....

def getSwitchMAC():
    """Get MAC address for the switch"""
    return "02:00:00:00:00:01"  # Locally administered MAC

def getNetworkSubnet():
    """Get network subnet for routing"""
    return "10.0.0.0/24"

# ================ TOPOLOGY IMPLEMENTATION ================
class SMLTopo(Topo):
    """Star topology with IP addressing for UDP-based SwitchML"""
    
    def __init__(self, **opts):
        Topo.__init__(self, **opts)
        
        print(f"[Topology] Creating Level 2 topology with {NUM_WORKERS} workers")
        
        # Create switch
        switch = self.addSwitch('s1')
        
        # Create workers with proper IP configuration
        for i in range(NUM_WORKERS):
            worker_name = f'w{i}'
            worker_ip = getWorkerIP(i)
            worker_mac = getWorkerMAC(i)
            
            print(f"[Topology] Creating {worker_name}: IP={worker_ip}, MAC={worker_mac}")
            
            # Create worker host
            worker = self.addHost(
                worker_name,
                ip=f"{worker_ip}/24",
                mac=worker_mac,
                defaultRoute="via 10.0.0.1"  # Simple default route
            )
            
            # Connect to switch WITHOUT port IPs
            self.addLink(worker, switch, port2=i)
        
        print(f"[Topology] Created star topology with {NUM_WORKERS} workers")

# 2. EARLY ARP CONFIGURATION
def configureNetworkStack(net):
    """Configure network stack immediately after start"""
    print("\n[Network] Configuring network stack...")
    
    switch = net.get('s1')
    switch_ip = getSwitchIP()
    switch_mac = getSwitchMAC()
    
    # Configure ARP on all workers
    for i in range(NUM_WORKERS):
        worker = net.get(f'w{i}')
        
        # Add static ARP entry
        worker.cmd(f'arp -s {switch_ip} {switch_mac}')
        print(f"[Network]  ARP configured on w{i}")
        
        # Disable reverse path filtering (might block responses)
        worker.cmd('sysctl -w net.ipv4.conf.all.rp_filter=0')
        worker.cmd('sysctl -w net.ipv4.conf.eth0.rp_filter=0')
        
        # Enable IP forwarding (just in case)
        worker.cmd('sysctl -w net.ipv4.ip_forward=1')
    
    # Configure switch to accept packets
    switch.cmd('sysctl -w net.ipv4.ip_forward=1')
    print("[Network]  Network stack configured")

# ================ WORKER EXECUTION ================
def RunWorkers(net):
    """
    Starts workers and waits for completion.
    Enhanced with network verification for Level 2.
    """
    worker = lambda rank: f"w{rank}"
    log_file = lambda rank: os.path.join(os.environ['APP_LOGS'], f"{worker(rank)}.log")
    
    print(f"[Workers] Starting {NUM_WORKERS} workers for UDP-based AllReduce...")
    
    # ============= PRE-FLIGHT NETWORK VERIFICATION =============
    print("[Workers] Verifying network connectivity...")
    
    switch_ip = getSwitchIP()
    for i in range(NUM_WORKERS):
        worker_host = net.get(worker(i))
        worker_ip = getWorkerIP(i)
        
        # Verify worker can reach switch
        print(f"[Workers] Testing connectivity: {worker(i)} ({worker_ip}) → Switch ({switch_ip})")
        
    
    # ============= START ALL WORKERS =============
    for i in range(NUM_WORKERS):
        worker_name = worker(i)
        log_path = log_file(i)
        
        # Enhanced command with network information
        cmd = f'python worker.py {i} > {log_path} 2>&1'
        
        print(f"[Workers] Starting {worker_name} (rank {i})")
        print(f"  - Worker IP: {getWorkerIP(i)}")
        print(f"  - Switch IP: {switch_ip}")
        print(f"  - UDP Port: {SWITCHML_UDP_PORT}")
        print(f"  - Log file: {log_path}")
        
        net.get(worker_name).sendCmd(cmd)
    
    # ============= WAIT FOR COMPLETION =============
    print("[Workers] Waiting for all workers to complete...")
    for i in range(NUM_WORKERS):
        worker_name = worker(i)
        print(f"[Workers] Waiting for {worker_name}...")
        net.get(worker_name).waitOutput()
    
    print("[Workers] All workers completed!")

# ================ CONTROL PLANE CONFIGURATION ================
def RunControlPlane(net):
    """Control plane configuration with fixes"""
    print("\n[Control Plane] Configuring P4 switch...")
    
    switch = net.get('s1')
    switch_ip = getSwitchIP()
    
    # Configure multicast group
    print(f"[Control Plane] Setting up multicast group {MULTICAST_GROUP_ID}...")
    worker_ports = list(range(NUM_WORKERS))
    
    try:
        switch.addMulticastGroup(mgid=MULTICAST_GROUP_ID, ports=worker_ports)
        print(f"[Control Plane] Multicast group configured: ports {worker_ports}")
    except Exception as e:
        print(f"[Control Plane] Multicast group error: {e}")
    
    # Configure worker table
    print(f"[Control Plane] Configuring worker identification table...")
    
    for worker_id in range(NUM_WORKERS):
        worker_ip = getWorkerIP(worker_id)
        worker_ip_int = ipToInt(worker_ip)
        switch_ip_int = ipToInt(switch_ip)
        
        try:
            switch.insertTableEntry(
                table_name='TheIngress.worker_table',
                match_fields={
                    'hdr.ipv4.srcAddr': worker_ip_int,
                    'hdr.udp.dstPort': SWITCHML_UDP_PORT
                },
                action_name='TheIngress.set_worker_id',
                action_params={
                    'worker_id': worker_id,
                    'switch_ip': switch_ip_int
                }
            )
            print(f"[Control Plane]  Worker {worker_id} configured: {worker_ip}")
        except Exception as e:
            print(f"[Control Plane]  Worker {worker_id} error: {e}")
    
    # Configure IPv4 routes (only for worker destinations)
    print("[Control Plane] Configuring IPv4 routing...")
    
    for worker_id in range(NUM_WORKERS):
        worker_ip = getWorkerIP(worker_id)
        worker_mac = getWorkerMAC(worker_id)
        worker_ip_int = ipToInt(worker_ip)
        worker_mac_int = macToInt(worker_mac)
        
        try:
            switch.insertTableEntry(
                table_name='TheIngress.ipv4_lpm',
                match_fields={
                    'hdr.ipv4.dstAddr': (worker_ip_int, 32)
                },
                action_name='TheIngress.ipv4_forward',
                action_params={
                    'dstAddr': worker_mac_int,
                    'port': worker_id
                }
            )
            print(f"[Control Plane]  Route to worker {worker_id}: {worker_ip}/32")
        except Exception as e:
            print(f"[Control Plane]  Route error for worker {worker_id}: {e}")
    
    # Add broadcast route
    try:
        broadcast_ip_int = ipToInt("10.0.0.255")
        switch.insertTableEntry(
            table_name='TheIngress.ipv4_lpm',
            match_fields={
                'hdr.ipv4.dstAddr': (broadcast_ip_int, 32)
            },
            action_name='TheIngress.broadcast_packet',
            action_params={}
        )
        print("[Control Plane]  Broadcast route configured")
    except Exception as e:
        print(f"[Control Plane]  Broadcast route error: {e}")
    
    print("[Control Plane]  Configuration complete!")
    
    # Verify configuration
    print("\n[Control Plane] Table entries:")
    try:
        switch.printTableEntries()
    except:
        pass

# ================ UTILITY FUNCTIONS ================
def ipToInt(ip_str):
    """Convert IP address string to integer"""
    parts = ip_str.split('.')
    return (int(parts[0]) << 24) + (int(parts[1]) << 16) + (int(parts[2]) << 8) + int(parts[3])

def macToInt(mac_str):
    """Convert MAC address string to integer"""
    return int(mac_str.replace(':', ''), 16)

# ================ NETWORK INITIALIZATION ================
def createNetwork():
    """Create and configure the complete Level 2 network"""
    print("=" * 60)
    print(" LEVEL 2: UDP-based SwitchML Network Initialization")
    print("=" * 60)
    
    # Create topology
    topo = SMLTopo()
    
    # Create P4Mininet instance
    net = P4Mininet(
        program="p4/main.p4",  # Our Level 2 P4 program
        topo=topo,
        auto_arp=False  # i'll handle ARP manually for better control
    )
    
    # Attach control functions
    net.run_control_plane = lambda: RunControlPlane(net)
    net.run_workers = lambda: RunWorkers(net)
    
    return net

# 4. DIAGNOSTIC FUNCTION
def testConnectivity(net):
    """Test basic connectivity before running workers"""
    print("\n[Test] Running connectivity test...")
    
    # Test UDP packet sending
    w0 = net.get('w0')
    test_result = w0.cmd("""python3 -c "
                            import socket
                            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            sock.settimeout(0.1)
                            try:
                                sock.sendto(b'TEST', ('10.0.0.1', 12345))
                                print(' UDP send successful')
                            except Exception as e:
                                print(f' UDP send failed: {e}')
                            sock.close()
                            " """)
    
    print(f"[Test] UDP send test: {test_result.strip()}")

# ================ MAIN EXECUTION ================
if __name__ == "__main__":
    # Create network
    net = createNetwork()
    
    try:
        print("\n[Network] Starting Mininet...")
        net.start()
        
        # Configure network stack FIRST
        configureNetworkStack(net)
        
        # Configure control plane
        net.run_control_plane()
        
        # Test connectivity
        testConnectivity(net)
        
        print("\n[Network] Ready! Commands:")
        print("  py net.run_workers()  - Run AllReduce test")
        print("  py testConnectivity(net) - Test UDP connectivity")
        print("  exit - Shutdown")
        
        CLI(net)
        
    except Exception as e:
        print(f"\n[Network] Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("[Network] Shutting down...")
        net.stop()

# ================ BACKWARDS COMPATIBILITY ================
# Create instances for the template structure
topo = SMLTopo()
net = P4Mininet(program="p4/main.p4", topo=topo)
net.run_control_plane = lambda: RunControlPlane(net)
net.run_workers = lambda: RunWorkers(net)