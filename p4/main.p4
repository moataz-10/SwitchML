

#include <core.p4>
#include <v1model.p4>

// ================ CONSTANTS ================
const bit<16> ETHERTYPE_IPV4 = 0x0800;
const bit<16> ETHERTYPE_ARP = 0x0806;
const bit<8>  IP_PROTOCOL_UDP = 17;
const bit<16> SWITCHML_UDP_PORT = 12345;  // Custom UDP port for SwitchML
const bit<16> MULTICAST_GROUP_ID = 1;
const bit<8>  NUM_WORKERS = 4;
const bit<8>  CHUNK_SIZE = 4;

// ================ TYPE DEFINITIONS ================
typedef bit<9>  sw_port_t;   
typedef bit<48> mac_addr_t;  
typedef bit<32> ip4_addr_t;  
typedef bit<32> worker_id_t;
typedef bit<32> chunk_id_t;
typedef bit<32> value_t;

// ================ HEADER DEFINITIONS ================

header ethernet_t {
    mac_addr_t dstAddr;
    mac_addr_t srcAddr;
    bit<16>    etherType;
}

header ipv4_t {
    bit<4>      version;
    bit<4>      ihl;
    bit<8>      diffserv;
    bit<16>     totalLen;
    bit<16>     identification;
    bit<3>      flags;
    bit<13>     fragOffset;
    bit<8>      ttl;
    bit<8>      protocol;
    bit<16>     hdrChecksum;
    ip4_addr_t  srcAddr;
    ip4_addr_t  dstAddr;
}

header udp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<16> length;
    bit<16> checksum;
}

//the customized header where highlights the main feature in using the P4-Programmable data plan 
header sml_t {
    worker_id_t worker_id;    // Which worker sent this
    chunk_id_t  chunk_id;     // Which chunk this is
    bit<32>     num_values;   // Number of values (always CHUNK_SIZE)
    value_t     value1;       // Aggregation values
    value_t     value2;
    value_t     value3;
    value_t     value4;
}

// ================ HEADERS STRUCT ================
struct headers {
    ethernet_t eth;
    ipv4_t     ipv4;
    udp_t      udp;
    sml_t      sml;
}

// ================ METADATA ================
struct metadata {
    bit<8>      current_worker_id;
    chunk_id_t  current_chunk_id;
    bit<8>      workers_seen;
    ip4_addr_t  switch_ip;
}

// ================ PARSER IMPLEMENTATION ================
parser TheParser(packet_in packet,
                 out headers hdr,
                 inout metadata meta,
                 inout standard_metadata_t standard_metadata) {
    
    state start {
        transition parse_ethernet;
    }
    
    state parse_ethernet {
        packet.extract(hdr.eth);
        transition select(hdr.eth.etherType) {
            ETHERTYPE_IPV4: parse_ipv4;
            ETHERTYPE_ARP:  accept;       // Accept ARP for address resolution
            default:        accept;       // Accept other protocols
        }
    }
    
    state parse_ipv4 {
        packet.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            IP_PROTOCOL_UDP: parse_udp;
            default:         accept;      // Accept other IP protocols
        }
    }
    
    state parse_udp {
        packet.extract(hdr.udp);
        transition select(hdr.udp.dstPort) {
            SWITCHML_UDP_PORT: parse_sml;  // SwitchML packets
            default:           accept;      // Regular UDP traffic
        }
    }
    
    state parse_sml {
        packet.extract(hdr.sml);
        transition accept;
    }
}

// ================ INGRESS CONTROL ================
control TheIngress(inout headers hdr,
                   inout metadata meta,
                   inout standard_metadata_t standard_metadata) {
    
    // ============= REGISTERS FOR AGGREGATION =============
    register<value_t>(1024) chunk_sum1;
    register<value_t>(1024) chunk_sum2;
    register<value_t>(1024) chunk_sum3;
    register<value_t>(1024) chunk_sum4;
    register<bit<8>>(1024)  chunk_workers;
    
    // ============= BASIC ACTIONS =============
    action drop() {
        mark_to_drop(standard_metadata);
    }
    
    action set_worker_id(worker_id_t worker_id, ip4_addr_t switch_ip) {
        meta.current_worker_id = (bit<8>)worker_id;
        meta.switch_ip = switch_ip;
    }
    
    // ============= L3 FORWARDING ACTIONS =============
    action ipv4_forward(mac_addr_t dstAddr, sw_port_t port) {
        standard_metadata.egress_spec = port;
        hdr.eth.srcAddr = hdr.eth.dstAddr;  // Switch's MAC becomes source
        hdr.eth.dstAddr = dstAddr;          // Set destination MAC
        hdr.ipv4.ttl = hdr.ipv4.ttl - 1;    // Decrement TTL
    }
    
    action broadcast_packet() {
        standard_metadata.mcast_grp = MULTICAST_GROUP_ID;
    }
    
    // ============= SWITCHML ACTIONS =============
    action multicast_result() {
        // Change source IP to switch IP and swap UDP ports
        // Validate required headers
    hdr.eth.setValid();
    hdr.ipv4.setValid();
    hdr.udp.setValid();

    hdr.eth.dstAddr = 0xFFFFFFFFFFFF;

    // Set IPv4 header fields
    hdr.ipv4.srcAddr = meta.switch_ip;
    hdr.ipv4.dstAddr = 0x0A0000FF;  // Set to a group/broadcast address
    hdr.ipv4.protocol = 17;  // UDP
    hdr.ipv4.totalLen = 20 + 8 + 28;  // IP + UDP + SML payload size in bytes
    
    // Set UDP header fields
    hdr.udp.checksum = 0;
    hdr.udp.srcPort = 12345;  // SWITCHML_UDP_PORT
    hdr.udp.dstPort = 12345;  // SWITCHML_UDP_PORT
    hdr.udp.length = 8 + 28;  // UDP header + SML packet (7 * 4 bytes)
    standard_metadata.mcast_grp = MULTICAST_GROUP_ID;
    }
    
    // ============= AGGREGATION LOGIC =============
    action aggregate_chunk() {
        // Calculate safe chunk index
        bit<32> safe_chunk_id = meta.current_chunk_id;
        if (safe_chunk_id >= 1024) {
            safe_chunk_id = safe_chunk_id & 1023;  // Hardware-compliant modulo
        }
        
        // Temporary variables for atomic operation
        value_t current_sum1;
        value_t current_sum2;
        value_t current_sum3;
        value_t current_sum4;
        bit<8> current_workers;
        
        // Atomic read-modify-write
        @atomic {
            // Read current aggregation state
            chunk_sum1.read(current_sum1, safe_chunk_id);
            chunk_sum2.read(current_sum2, safe_chunk_id);
            chunk_sum3.read(current_sum3, safe_chunk_id);
            chunk_sum4.read(current_sum4, safe_chunk_id);
            chunk_workers.read(current_workers, safe_chunk_id);
            
            // Aggregate this worker's contribution
            current_sum1 = current_sum1 + hdr.sml.value1;
            current_sum2 = current_sum2 + hdr.sml.value2;
            current_sum3 = current_sum3 + hdr.sml.value3;
            current_sum4 = current_sum4 + hdr.sml.value4;
            current_workers = current_workers + 1;
            
            // Write back updated state
            chunk_sum1.write(safe_chunk_id, current_sum1);
            chunk_sum2.write(safe_chunk_id, current_sum2);
            chunk_sum3.write(safe_chunk_id, current_sum3);
            chunk_sum4.write(safe_chunk_id, current_sum4);
            chunk_workers.write(safe_chunk_id, current_workers);
            
            // Update packet with aggregated values
            hdr.sml.value1 = current_sum1;
            hdr.sml.value2 = current_sum2;
            hdr.sml.value3 = current_sum3;
            hdr.sml.value4 = current_sum4;
            
            // Store worker count for decision making
            meta.workers_seen = current_workers;
        }
    }
    
    action reset_chunk() {
        bit<32> safe_chunk_id = meta.current_chunk_id;
        if (safe_chunk_id >= 1024) {
            safe_chunk_id = safe_chunk_id & 1023;
        }
        
        @atomic {
            chunk_sum1.write(safe_chunk_id, 0);
            chunk_sum2.write(safe_chunk_id, 0);
            chunk_sum3.write(safe_chunk_id, 0);
            chunk_sum4.write(safe_chunk_id, 0);
            chunk_workers.write(safe_chunk_id, 0);
        }
    }
    
    // ============= MATCH-ACTION TABLES =============
    
    // Table to identify workers by their IP address
    table worker_table {
        key = {
            hdr.ipv4.srcAddr: exact;
            hdr.udp.dstPort: exact;
        }
        actions = {
            set_worker_id;
            drop;
            NoAction;
        }
        size = 32;
        default_action = drop();
    }
    
    // Table for IPv4 forwarding (handles regular IP traffic)
    table ipv4_lpm {
        key = {
            hdr.ipv4.dstAddr: lpm;
        }
        actions = {
            ipv4_forward;
            broadcast_packet;
            drop;
            NoAction;
        }
        size = 1024;
        default_action = drop();
    }
    
    // ============= MAIN APPLY LOGIC =============
    apply {
        // Only process valid Ethernet frames
        if (hdr.eth.isValid()) {
            
            // Check if this is a potential SwitchML packet (UDP to port 12345)
            if (hdr.ipv4.isValid() && hdr.udp.isValid() && hdr.udp.dstPort == SWITCHML_UDP_PORT) {
                
                // This is a SwitchML packet - must have valid SML header
                if (hdr.sml.isValid()) {
                    // Lookup worker by source IP
                    if (worker_table.apply().hit) {
                        // Extract chunk ID for aggregation
                        meta.current_chunk_id = hdr.sml.chunk_id;
                        
                        // Perform in-network aggregation
                        aggregate_chunk();
                        
                        // Check if aggregation is complete
                        if (meta.workers_seen == NUM_WORKERS) {
                            // All workers contributed - broadcast result
                            multicast_result();
                            reset_chunk();
                        } else {
                            // More workers expected - drop packet
                            drop();
                        }
                    } else {
                        // Unknown worker - drop packet
                        drop();
                    }
                } else {
                    // UDP packet to SwitchML port but invalid SML header - drop
                    drop();
                }
            }
            // Handle regular IPv4 traffic (routing, ARP responses, etc.)
            else if (hdr.ipv4.isValid()) {
                ipv4_lpm.apply();
            }
            // Handle non-IP traffic (ARP requests, etc.)
            else {
                broadcast_packet();
            }
        } else {
            // Invalid Ethernet frame
            drop();
        }
    }
}

// ================ EGRESS CONTROL ================
control TheEgress(inout headers hdr,
                  inout metadata meta,
                  inout standard_metadata_t standard_metadata) {
    apply {
        // No special egress processing needed for Level 2
        // Could add egress-specific logic here if needed
    }
}

// ================ CHECKSUM CONTROLS ================
control TheChecksumVerification(inout headers hdr, inout metadata meta) {
    apply {
        // Verify IPv4 header checksum
        verify_checksum(
            hdr.ipv4.isValid(),
            {
                hdr.ipv4.version,
                hdr.ipv4.ihl,
                hdr.ipv4.diffserv,
                hdr.ipv4.totalLen,
                hdr.ipv4.identification,
                hdr.ipv4.flags,
                hdr.ipv4.fragOffset,
                hdr.ipv4.ttl,
                hdr.ipv4.protocol,
                hdr.ipv4.srcAddr,
                hdr.ipv4.dstAddr
            },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16
        );
    }
}

control TheChecksumComputation(inout headers hdr, inout metadata meta) {
    apply {
        // Update IPv4 header checksum
        update_checksum(
            hdr.ipv4.isValid(),
            {
                hdr.ipv4.version,
                hdr.ipv4.ihl,
                hdr.ipv4.diffserv,
                hdr.ipv4.totalLen,
                hdr.ipv4.identification,
                hdr.ipv4.flags,
                hdr.ipv4.fragOffset,
                hdr.ipv4.ttl,
                hdr.ipv4.protocol,
                hdr.ipv4.srcAddr,
                hdr.ipv4.dstAddr
            },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16
        );
    }
}

// ================ DEPARSER ================
control TheDeparser(packet_out packet, in headers hdr) {
    apply {
        packet.emit(hdr.eth);
        packet.emit(hdr.ipv4);
        packet.emit(hdr.udp);
        packet.emit(hdr.sml);
    }
}

// ================ MAIN SWITCH ================
V1Switch(
    TheParser(),
    TheChecksumVerification(),
    TheIngress(),
    TheEgress(),
    TheChecksumComputation(),
    TheDeparser()
) main;