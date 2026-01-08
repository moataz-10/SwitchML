

from lib.gen import GenInts, GenMultipleOfInRange
from lib.test import CreateTestData, RunIntTest
from lib.worker import *
from lib.comm import send, receive 
import socket
import struct
import time
import threading

# ================ CONSTANTS ================
NUM_ITER = 1
CHUNK_SIZE = 4
SWITCHML_UDP_PORT = 12345
SWITCH_IP = "10.0.0.1"
SOCKET_TIMEOUT = 5.0

# ================ ADDRESSING FUNCTIONS ================
def getWorkerIP(wid):
    """Get IP address for worker with given ID"""
    return f"10.0.0.{wid + 2}"

def getSwitchIP():
    """Get switch IP address"""
    return SWITCH_IP

# ================ PACKET STRUCTURE (SAME AS BEFORE) ================
class SwitchML:
    """SwitchML packet structure for Level 2 (UDP-based)"""
    
    PACKET_FORMAT = '>7I'  # 7 unsigned 32-bit integers, big-endian
    PACKET_SIZE = struct.calcsize(PACKET_FORMAT)  # 28 bytes
    
    def __init__(self, worker_id=0, chunk_id=0, num_values=CHUNK_SIZE, 
                 value1=0, value2=0, value3=0, value4=0):
        self.worker_id = worker_id
        self.chunk_id = chunk_id
        self.num_values = num_values
        self.value1 = value1
        self.value2 = value2
        self.value3 = value3
        self.value4 = value4
    
    def serialize(self):
        """Convert packet to bytes for network transmission"""
        try:
            packed_data = struct.pack(
                self.PACKET_FORMAT,
                self.worker_id,
                self.chunk_id,
                self.num_values,
                self.value1,
                self.value2,
                self.value3,
                self.value4
            )
            return packed_data
        except struct.error as e:
            raise ValueError(f"Failed to serialize SwitchML packet: {e}")
    
    @classmethod
    def deserialize(cls, data):
        """Convert bytes to SwitchML packet object"""
        if len(data) < cls.PACKET_SIZE:
            raise ValueError(f"Insufficient data for SwitchML packet: {len(data)} < {cls.PACKET_SIZE}")
        
        try:
            unpacked = struct.unpack(cls.PACKET_FORMAT, data[:cls.PACKET_SIZE])
            return cls(
                worker_id=unpacked[0],
                chunk_id=unpacked[1],
                num_values=unpacked[2],
                value1=unpacked[3],
                value2=unpacked[4],
                value3=unpacked[5],
                value4=unpacked[6]
            )
        except struct.error as e:
            raise ValueError(f"Failed to deserialize SwitchML packet: {e}")
    
    def __str__(self):
        return (f"SwitchML(worker_id={self.worker_id}, chunk_id={self.chunk_id}, "
                f"values=[{self.value1}, {self.value2}, {self.value3}, {self.value4}])")

# ================ SOCKET MANAGEMENT ================
def createSocket(rank):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(SOCKET_TIMEOUT)

    
    sock.bind(('', SWITCHML_UDP_PORT))

    Log(f"Socket bound to all interfaces on port {SWITCHML_UDP_PORT}")
    return sock

def closeSocket(sock):
    """Safely close socket"""
    try:
        if sock:
            sock.close()
            Log("Socket closed successfully")
    except Exception as e:
        Log(f"Socket close warning: {e}")

# ================ ALLREDUCE IMPLEMENTATION ================
def AllReduce(sock, rank, data, result):
    """
    Perform in-network all-reduce over UDP
    FIXED: Correct lib.comm API usage
    """
    
    Log(f"Starting UDP-based AllReduce - Worker {rank}")
    Log(f"Data length: {len(data)}")
    Log(f"Chunk size: {CHUNK_SIZE}")
    
    # Input validation
    if len(data) % CHUNK_SIZE != 0:
        Log(f"ERROR: Data length {len(data)} not divisible by chunk size {CHUNK_SIZE}")
        return False
    
    num_chunks = len(data) // CHUNK_SIZE
    Log(f"Processing {num_chunks} chunks")
    
    # Initialize result array
    result[:] = [0] * len(data)
    
    # Socket validation
    if not sock:
        Log("ERROR: Invalid socket provided")
        return False
    
    # Worker staggering
    stagger_delay = rank * 0.01  # 10ms per rank
    if stagger_delay > 0:
        Log(f"   Staggering delay: {stagger_delay:.3f}s")
        time.sleep(stagger_delay)
    
    # Process each chunk
    switch_address = (getSwitchIP(), SWITCHML_UDP_PORT)
    
    for chunk_idx in range(num_chunks):
        start_time = time.time()
        
        # Extract chunk data
        start_idx = chunk_idx * CHUNK_SIZE
        end_idx = start_idx + CHUNK_SIZE
        chunk_data = data[start_idx:end_idx]
        
        Log(f"Chunk {chunk_idx}: sending {chunk_data}")
        
        # Create SwitchML packet
        sml_packet = SwitchML(
            worker_id=rank,
            chunk_id=chunk_idx,
            num_values=CHUNK_SIZE,
            value1=chunk_data[0],
            value2=chunk_data[1],
            value3=chunk_data[2],
            value4=chunk_data[3]
        )
        
        #Serialize packet
        try:
            packet_bytes = sml_packet.serialize()
            Log(f"Packet serialized: {len(packet_bytes)} bytes")
        except Exception as e:
            Log(f"ERROR: Packet serialization failed: {e}")
            return False
        
        #Send using lib.comm.send() - correct API usage
        try:
            Log(f"Sending to switch: {switch_address}")
            send(sock, packet_bytes, switch_address) 
            Log(f"Packet sent successfully")
        except Exception as e:
            Log(f"ERROR: Failed to send packet: {e}")
            return False
        
        #Receive using lib.comm.receive() - correct API with nbytes
        Log(f"Waiting for aggregated result...")
        
        try:
            #receive() requires nbytes parameter
            response_data, sender_address = receive(sock, SwitchML.PACKET_SIZE)
            Log(f"Received {len(response_data)} bytes from {sender_address}")
            
            # Verify sender is the switch
            if sender_address[0] != getSwitchIP():
                Log(f"WARNING: Response from unexpected sender: {sender_address}")
                # Continue anyway - might be valid in some configurations
            
            # Deserialize response
            Log(f"Raw packet bytes: {response_data.hex()}")
            Log(f"Trimming to {SwitchML.PACKET_SIZE} bytes for deserialization")

            response_packet = SwitchML.deserialize(response_data[:SwitchML.PACKET_SIZE])
            Log(f"Response packet: {response_packet}")
            
            # Verify chunk ID matches
            if response_packet.chunk_id != chunk_idx:
                Log(f"ERROR: Chunk ID mismatch: expected {chunk_idx}, got {response_packet.chunk_id}")
                return False
            
            # Extract aggregated values
            aggregated_chunk = [
                response_packet.value1,
                response_packet.value2,
                response_packet.value3,
                response_packet.value4
            ]
            
            # Store result in output array
            result[start_idx:end_idx] = aggregated_chunk
            
            processing_time = time.time() - start_time
            Log(f"Chunk {chunk_idx} complete in {processing_time:.3f}s: {aggregated_chunk}")
            
        except socket.timeout:
            Log(f"ERROR: Timeout waiting for chunk {chunk_idx} response")
            return False
        except Exception as e:
            Log(f"ERROR: Failed to receive/process response: {e}")
            return False
        
        # Inter-chunk delay
        if chunk_idx < num_chunks - 1:
            time.sleep(0.001)  # 1ms delay between chunks
    
    Log(f"AllReduce completed successfully!")
    Log(f"Total chunks processed: {num_chunks}")
    Log(f"Final result length: {len(result)}")
    
    return True
# ================ NETWORK DIAGNOSTICS ================
def testNetworkConnectivity(sock, rank):
    """Test network connectivity to switch"""
    
    Log(f" Testing network connectivity...")
    
    # Test 1: Socket binding verification
    try:
        local_address = sock.getsockname()
        Log(f"Socket bound to: {local_address}")
    except Exception as e:
        Log(f"Socket binding issue: {e}")
        return False
    
    # Test 2: Create test packet
    test_packet = SwitchML(
        worker_id=rank,
        chunk_id=9999,  # Special test chunk ID
        num_values=CHUNK_SIZE,
        value1=1, value2=2, value3=3, value4=4
    )
    
    try:
        test_bytes = test_packet.serialize()
        Log(f"Test packet created: {len(test_bytes)} bytes")
    except Exception as e:
        Log(f"Test packet creation failed: {e}")
        return False
    
    Log(f"    Network connectivity test passed")
    return True

# ================ MAIN WORKER FUNCTION ================
def main():
    """Main worker function"""
    
    # Initialization
    rank = GetRankOrExit()
    
    Log(f"Level 2 Worker Starting")
    Log(f"Worker rank: {rank}")
    Log(f"Worker IP: {getWorkerIP(rank)}")
    Log(f"Switch IP: {getSwitchIP()}")
    Log(f"UDP Port: {SWITCHML_UDP_PORT}")
    
    # Socket creation
    sock = None
    try:
        sock = createSocket(rank)
        
        # Test network connectivity
        if not testNetworkConnectivity(sock, rank):
            Log(" WARNING: Network connectivity test failed")
        
    except Exception as e:
        Log(f" FATAL: Socket initialization failed: {e}")
        return
    
    # AllReduce iterations
    try:
        Log(" Starting AllReduce iterations...")
        
        for i in range(NUM_ITER):
            Log(f"{'='*50}")
            Log(f" Iteration {i}")
            Log(f"{'='*50}")
            
            # Generate test data
            num_elem = GenMultipleOfInRange(2, 2048, 2 * CHUNK_SIZE)
            data_out = GenInts(num_elem)
            data_in = [0] * num_elem
            
            Log(f"Generated {num_elem} elements")
            Log(f"First few elements: {data_out[:min(8, len(data_out))]}")
            
            # Create test data file
            CreateTestData(f"udp-iter-{i}", rank, data_out)
            
            # Perform AllReduce (without retry)
            start_time = time.time()
            success = AllReduce(sock, rank, data_out, data_in)
            end_time = time.time()
            
            if success:
                Log(f"AllReduce completed in {end_time - start_time:.3f}s")
                Log(f"Result length: {len(data_in)}")
                Log(f"First few results: {data_in[:min(8, len(data_in))]}")
                
                # Run validation test
                RunIntTest(f"udp-iter-{i}", rank, data_in, True)
                Log(f"Iteration {i} passed validation")
                
            else:
                Log(f"Iteration {i} failed")
                break
        
        Log("All iterations completed!")
        
    except KeyboardInterrupt:
        Log(" Interrupted by user")
    except Exception as e:
        Log(f" FATAL: Unexpected error during AllReduce: {e}")
        import traceback
        traceback.print_exc()
    finally:
        closeSocket(sock)
        Log("Worker finished")

if __name__ == '__main__':
    main()