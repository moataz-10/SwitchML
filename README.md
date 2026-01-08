
## 📖 Overview

This project implements **Level 2** of a three-level SwitchML learning progression, focusing on UDP-based in-network AllReduce operations. The implementation demonstrates how programmable network switches can accelerate distributed machine learning by performing gradient aggregation directly in the network dataplane.

### What is AllReduce?

AllReduce is a collective communication primitive where N workers each contribute a vector, and all workers receive the aggregated sum:

```
Worker 0: [1, 2, 3, 4]  ┐
Worker 1: [5, 6, 7, 8]  ├─→ Switch Aggregates ─→ All Workers: [6, 8, 10, 12]
Worker N: [...]         ┘
```

### Level 2 Features

- ✅ **UDP Communication**: Workers communicate with switch over UDP port 12345
- ✅ **In-Network Aggregation**: Switch aggregates values using P4 registers
- ✅ **Atomic Operations**: Race-condition-free aggregation using `@atomic` blocks
- ✅ **Multicast Delivery**: Efficient broadcast of results to all workers
- ✅ **Hardware Compliance**: Respects real Tofino switch constraints

## 🏗️ Architecture

### Network Topology

```
┌─────────────┐         ┌─────────────┐
│  Worker 0   │         │  Worker 1   │
│  10.0.0.2   │         │  10.0.0.3   │
│  Port 12345 │         │  Port 12345 │
└──────┬──────┘         └──────┬──────┘
       │                       │
       │    UDP Packets        │
       │    (SwitchML)         │
       └───────┬───────────────┘
               │
       ┌───────▼────────┐
       │  P4 Switch     │
       │  10.0.0.1      │
       │  In-Network    │
       │  Aggregation   │
       └────────────────┘
```

### System Components

| Component | Description | Technology |
|-----------|-------------|------------|
| **P4 Switch** | Performs in-network aggregation | P4 v1model, BMv2 |
| **Workers** | Send chunks and receive results | Python, UDP sockets |
| **Control Plane** | Configures switch tables and multicast | Python, P4Runtime |
| **Network** | Star topology simulation | Mininet |

## 🚀 Getting Started

### Prerequisites

```bash
# Ubuntu 20.04+ recommended
sudo apt-get update
sudo apt-get install -y python3 python3-pip mininet

# Install P4 tools (if not already installed)
bash ./install_p4_env.sh
```

### Installation

```bash
# Clone the repository
git clone https://github.com/moataz-10/SwitchML.git
cd switchml-level2

# Install Python dependencies
pip3 install -r requirements.txt
```

### Running the Implementation

1. **Start the network:**
```bash
sudo ./start.sh
```

This will:
- Compile the P4 program (`p4/main.p4`)
- Create Mininet topology with workers and switch
- Configure control plane (tables, multicast groups, routing)
- Open Mininet CLI

2. **Run AllReduce test:**
```bash
mininet> py net.run_workers()
```

3. **View results:**
```bash
# Check worker logs
cat logs/w0.log
cat logs/w1.log

# Check test results
cat logs/test/udp-iter-0/test-rank-0.txt
```

## 📋 Implementation Details

### Protocol Flow

```
1. Worker chunks data into groups of 4 values
   ├─ Chunk 0: [v1, v2, v3, v4]
   ├─ Chunk 1: [v5, v6, v7, v8]
   └─ ...

2. For each chunk:
   ├─ Worker sends UDP packet to switch (10.0.0.1:12345)
   ├─ Switch performs atomic aggregation
   ├─ When all workers contribute: multicast result
   └─ All workers receive identical aggregated values

3. Workers proceed to next chunk
```

### SwitchML Packet Format (28 bytes)

```
┌──────────────┬──────────────┬──────────────┬──────────────────────────────┐
│  worker_id   │   chunk_id   │  num_values  │     4 x 32-bit values        │
│   (4 bytes)  │   (4 bytes)  │   (4 bytes)  │        (16 bytes)            │
└──────────────┴──────────────┴──────────────┴──────────────────────────────┘
```

### Key Design Decisions

#### 1. Chunk Size: 4 Values
- **Rationale**: Balances packet efficiency with hardware constraints
- **Benefits**: 
  - 5 registers (4 values + 1 counter) fit in single pipeline stage
  - 28-byte packets minimize network overhead
  - Atomic operation complexity stays under hardware limits

#### 2. Single-Slot Protocol
- **Behavior**: One chunk in flight at a time per worker
- **Benefits**:
  - Simple synchronization
  - Minimal switch state
  - Easy debugging and verification
- **Trade-off**: Lower throughput vs. multi-slot variants

#### 3. UDP Transport
- **Port**: 12345 (distinguishes SwitchML traffic)
- **Benefits**:
  - Realistic L4 communication
  - Socket-based programming model
  - Prepares for Level 3 reliability mechanisms

## 🔧 Core Components

### 1. P4 Switch Program (`p4/main.p4`)

#### Parser Pipeline
```p4
Ethernet → IPv4 → UDP → SwitchML Header
```

#### Atomic Aggregation
```p4
@atomic {
    // Read current state
    chunk_sum1.read(current_sum1, chunk_id);
    chunk_workers.read(current_workers, chunk_id);
    
    // Aggregate
    current_sum1 = current_sum1 + hdr.sml.value1;
    current_workers = current_workers + 1;
    
    // Write back
    chunk_sum1.write(chunk_id, current_sum1);
    chunk_workers.write(chunk_id, current_workers);
}
```

#### Decision Logic
```p4
if (workers_seen == NUM_WORKERS) {
    multicast_result();  // Broadcast to all workers
    reset_chunk();       // Clear for reuse
} else {
    drop();             // Wait for more workers
}
```

### 2. Worker Implementation (`worker.py`)

#### AllReduce Function
```python
def AllReduce(sock, rank, data, result):
    for chunk_idx in range(num_chunks):
        # Send chunk to switch
        packet = SwitchML(worker_id=rank, chunk_id=chunk_idx, 
                         values=chunk_data)
        send(sock, packet.serialize(), (SWITCH_IP, 12345))
        
        # Wait for aggregated result
        response = receive(sock, PACKET_SIZE)
        
        # Store aggregated values
        result[chunk_idx*4:(chunk_idx+1)*4] = response.values
```

### 3. Network & Control Plane (`network.py`)

#### Topology Setup
```python
- Switch: 10.0.0.1
- Worker 0: 10.0.0.2
- Worker 1: 10.0.0.3
- Static ARP entries for fast resolution
```

#### Control Plane Configuration
```python
# Worker identification
switch.insertTableEntry('worker_table',
    match={'srcAddr': worker_ip},
    action='set_worker_id')

# Multicast group
switch.addMulticastGroup(mgid=1, ports=[0, 1, ...])

# IPv4 routing
switch.insertTableEntry('ipv4_lpm',
    match={'dstAddr': worker_ip},
    action='ipv4_forward')
```

## 📊 Performance Characteristics

### Switch Efficiency
- **O(1) aggregation time** per packet
- **5 registers** per chunk (minimal state)
- **Single pipeline stage** processing
- **Atomic operations** prevent race conditions

### Network Efficiency
- **28-byte packets** (16 bytes payload + 12 bytes headers)
- **Multicast delivery** (no unicast storm)
- **Fixed-size packets** (no fragmentation)

### Scalability
- **Works with 1-8 workers** (configurable)
- **Arbitrary vector sizes** (multiples of chunk size)
- **Memory usage**: Linear with concurrent chunks (1024 max)

## 🧪 Testing

### Test Framework
The implementation includes comprehensive testing infrastructure:

```python
# Generate test data
data_out = GenInts(num_elements)

# Perform AllReduce
AllReduce(sock, rank, data_out, data_in)

# Verify correctness
RunIntTest(test_name, rank, data_in, verify=True)
```

### Verification
- **Mathematical correctness**: Validates sum across all workers
- **Chunk ordering**: Ensures proper sequential processing
- **Edge cases**: Tests various vector sizes and worker counts

### Example Test Output
```
[+] Running test: udp-iter-0, rank: 0
[+] From data files:
    data-rank-0.csv
    data-rank-1.csv
[+] Result: PASS
```

## 📝 Key Technical Highlights

### 1. Hardware Compliance
✅ **Single register access per atomic block**
- Each of 5 registers accessed exactly once
- No multiplication/division/modulo operations
- Respects RMT architecture constraints

### 2. Multicast Mechanism
✅ **Efficient result distribution**
- P4 multicast groups for hardware replication
- IPv4 routing for delivery to each worker
- Static ARP prevents resolution delays
- Broadcast-enabled sockets on workers

### 3. Correctness Guarantees
✅ **Atomic aggregation**
- Prevents race conditions
- Ensures read-modify-write atomicity
- Worker counting prevents double-aggregation

## 🔍 Common Issues & Solutions

### Issue 1: Workers not receiving multicast packets
**Solution**: Ensure sockets are configured with `SO_BROADCAST` and bound to all interfaces (`0.0.0.0`)

### Issue 2: ARP resolution delays
**Solution**: Static ARP entries configured in `configureNetworkStack()`

### Issue 3: Endianness mismatches
**Solution**: Big-endian serialization (`struct.pack('>7I', ...)`)

### Issue 4: Port conflicts
**Solution**: `SO_REUSEADDR` socket option enabled

## 📚 Project Structure

```
.
├── p4/
│   └── main.p4              # P4 switch program
├── worker.py                # Worker implementation
├── network.py               # Topology & control plane
├── lib/
│   ├── comm.py             # Communication utilities
│   ├── worker.py           # Worker utilities
│   ├── test.py             # Testing framework
│   └── gen.py              # Test data generation
├── logs/                    # Runtime logs and pcaps
├── start.sh                # Network startup script
└── README.md               # This file
```

## 🎓 Learning Objectives Achieved

This Level 2 implementation demonstrates:

✅ **P4 Programming**: Parser, match-action tables, registers, atomic operations  
✅ **UDP Networking**: Socket programming, multicast, endianness  
✅ **Distributed Systems**: AllReduce semantics, synchronization  
✅ **Control Plane**: Table configuration, multicast groups, routing  
✅ **Hardware Awareness**: Register access patterns, pipeline constraints  

## 🚧 Future Work (Level 3)

Level 3 extends this implementation with **reliability mechanisms**:
- Timeout and retry logic for packet loss
- Worker bitmap tracking (duplicate detection)
- Result caching for retransmissions
- Unicast responses for missed multicasts
- Enhanced state management on switch

## 📖 References

1. [SwitchML Paper (NSDI'21)](https://www.usenix.org/conference/nsdi21/presentation/sapio)
2. [P4 Language Specification](https://p4.org/p4-spec/docs/P4-16-v-1.2.3.html)
3. [AllReduce Explained](https://tech.preferred.jp/en/blog/technologies-behind-distributed-deep-learning-allreduce/)
4. [Intel Tofino Architecture](https://github.com/barefootnetworks/Open-Tofino)

## 🤝 Acknowledgments

This implementation is based on the lab assignment from the Advanced Networked Systems course. The original SwitchML system was developed by researchers at KAUST, Microsoft, Barefoot Networks, and University of Washington.

## 📄 License

This project is developed for educational purposes as part of university coursework.

---

**Course**: Advanced Networked Systems (SS25)  
**Lab**: Lab3 - Switches Do Dream of Machine Learning  
**Level**: Level 2 - In-network AllReduce over UDP  
**Author**: [Your Name]  
**Date**: January 2026