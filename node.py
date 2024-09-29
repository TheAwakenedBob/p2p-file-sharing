import socket
import threading
import os
import traceback
import time
import hashlib
from file_utils import chunk_file, reassemble_file, compute_sha256

CHUNK_SIZE = 512  # Size of each chunk

class Node:
    def __init__(self, port):
        self.port = port
        self.chunks = []  # To hold the actual chunks
        self.bitfield = []  # To track available chunks
        self.chunk_hashes = []
        self.server_thread = threading.Thread(target=self.start_server)
        self.server_thread.daemon = True  # Daemonize thread to end with main program
        self.server_thread.start()
        self.uploaded_chunks = 0
        self.downloaded_chunks = 0
        self.uploaded_files = 0
        self.downloaded_files = 0
        self.total_uploaded_bytes = 0
        self.total_downloaded_bytes = 0
        self.successful_connections = 0
        self.failed_connections = 0

    def start_server(self):
        """Starts a peer server that listens for incoming connections."""
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.bind(('0.0.0.0', self.port))  # Bind to all network interfaces
        server_socket.listen(5)  # Listen for up to 5 connections
        print(f"Peer listening on port {self.port}...")

        while True:
            try:
                conn, addr = server_socket.accept()  # Accept new connections
                print(f"Connected by {addr}")
                self.successful_connections += 1
                threading.Thread(target=self.handle_incoming_client, args=(conn,)).start()
            except Exception:
                self.failed_connections += 1
                print(f"Failed to establish connection")

    def verify_chunk(self, chunk_index, chunk_data):
        """Verify the integrity of a chunk using its hash."""
        chunk_hash = hashlib.sha256(chunk_data).hexdigest()
        return chunk_hash == self.chunk_hashes[chunk_index]

    def handle_incoming_client(self, conn):
        """Handles messages from incoming connections."""
        try:
            # Receive file name
            file_name = conn.recv(1024).decode().strip()
            print(f"Receiving file: {file_name}")

            # Receive the number of chunks and chunk hashes
            num_chunks = int(conn.recv(1024).decode())
            self.chunk_hashes = [conn.recv(64).decode() for _ in range(num_chunks)]
            print(f"Expecting {num_chunks} chunks.")

            # Initialize the local bitfield
            self.bitfield = [0] * num_chunks
            self.chunks = [None] * num_chunks

            # Send acknowledgment for setup
            conn.sendall("READY".encode())

            # Receive and process chunks
            corrupted_chunks = []
            for i in range(num_chunks):
                chunk_index_bytes = conn.recv(4)
                chunk_index = int.from_bytes(chunk_index_bytes, byteorder='big')
                chunk_size_bytes = conn.recv(4)
                chunk_size = int.from_bytes(chunk_size_bytes, byteorder='big')
                
                chunk_data = b''
                while len(chunk_data) < chunk_size:
                    packet = conn.recv(min(4096, chunk_size - len(chunk_data)))
                    if not packet:
                        raise Exception("Connection closed while receiving chunk data")
                    chunk_data += packet

                print(f"Received chunk {chunk_index} (size: {chunk_size} bytes)")
                
                if self.verify_chunk(chunk_index, chunk_data):
                    self.chunks[chunk_index] = chunk_data
                    self.bitfield[chunk_index] = 1
                    conn.sendall("ACK".encode())
                    self.downloaded_chunks += 1
                    self.total_downloaded_bytes += chunk_size
                else:
                    print(f"Chunk {chunk_index} is corrupted")
                    corrupted_chunks.append(chunk_index)
                    conn.sendall("NACK".encode())

            for chunk_index in corrupted_chunks:
                conn.sendall(f"RESEND {chunk_index}".encode())
                chunk_size_bytes = conn.recv(4)
                chunk_size = int.from_bytes(chunk_size_bytes, byteorder='big')
                
                chunk_data = b''
                while len(chunk_data) < chunk_size:
                    packet = conn.recv(min(4096, chunk_size - len(chunk_data)))
                    if not packet:
                        raise Exception("Connection closed while receiving chunk data")
                    chunk_data += packet

                if self.verify_chunk(chunk_index, chunk_data):
                    self.chunks[chunk_index] = chunk_data
                    self.bitfield[chunk_index] = 1
                    conn.sendall("ACK".encode())
                    self.downloaded_chunks += 1
                    self.total_downloaded_bytes += chunk_size
                else:
                    print(f"Chunk {chunk_index} is still corrupted after retransmission")

            # Receive the original hash to verify integrity
            original_hash = conn.recv(64).decode()

            # Reassemble file
            output_path = os.path.join('received_files', file_name)
            reassemble_file(self.chunks, output_path, original_hash)
            print(f"File {file_name} received and reassembled successfully.")

            self.downloaded_files += 1

        except Exception as e:
            print(f"Error while handling incoming client: {e}")
            print(traceback.format_exc())
            self.failed_connections += 1
        finally:
            conn.close()

    def connect_to_peer(self, ip, port, file):
        """Connects to another peer to upload a file."""
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client_socket.connect((ip, port))
            print(f"Connected to peer {ip}:{port}")
            self.successful_connections += 1

            # Send the file name
            file_name = os.path.basename(file)
            client_socket.sendall(file_name.encode())

            # Chunk the file and store chunks
            chunks = chunk_file(file)
            self.chunks = chunks
            num_chunks = len(chunks)

            # Send the number of chunks
            client_socket.sendall(str(num_chunks).encode())

            # Get chunk hashes by calc then sending
            chunk_hashes = [hashlib.sha256(chunk).hexdigest() for chunk in chunks]
            for chunk_hash in chunk_hashes:
                client_socket.sendall(chunk_hash.encode())

            # Wait for receiver to be ready
            ready_signal = client_socket.recv(1024).decode()
            if ready_signal != "READY":
                raise Exception("Receiver not ready")

            # Send chunks
            for i, chunk in enumerate(chunks):
                chunk_index_bytes = i.to_bytes(4, byteorder='big')
                chunk_size_bytes = len(chunk).to_bytes(4, byteorder='big')
                
                client_socket.sendall(chunk_index_bytes)
                client_socket.sendall(chunk_size_bytes)
                client_socket.sendall(chunk)
                
                print(f"Sent chunk {i} (size: {len(chunk)} bytes)")
                
                ack = client_socket.recv(1024).decode()
                if ack != "ACK":
                    print(f"Chunk {i} not acknowledged by peer: {ack}")
                    # Waiting for resend req (potentially, not guarenteed)
                    resend_request = client_socket.recv(1024).decode()
                    if resend_request.startswith("RESEND"):
                        chunk_index = int(resend_request.split()[1])
                        chunk = chunks[chunk_index]
                        chunk_size_bytes = len(chunk).to_bytes(4, byteorder='big')
                        client_socket.sendall(chunk_size_bytes)
                        client_socket.sendall(chunk)
                        print(f"Resent chunk {chunk_index}")
                        ack = client_socket.recv(1024).decode()
                        if ack != "ACK":
                            print(f"Chunk {chunk_index} still not acknowledged after resend")
                    else:
                        print(f"Unexpected response: {resend_request}")
                        break

                self.uploaded_chunks += 1
                self.total_uploaded_bytes += len(chunk)

            # Send the original file hash
            original_hash = compute_sha256(file)
            client_socket.sendall(original_hash.encode())

            print(f"File {file_name} sent successfully.")
            self.uploaded_files += 1

        except Exception as e:
            print(f"An error occurred while connecting to peer: {e}")
            print(traceback.format_exc())
            self.failed_connections += 1
        finally:
            client_socket.close()

    def print_statistics(self):
        """Print network statistics"""
        print("\n--- Network Statistics ---")
        print(f"Uploaded chunks: {self.uploaded_chunks}")
        print(f"Downloaded chunks: {self.downloaded_chunks}")
        print(f"Uploaded files: {self.uploaded_files}")
        print(f"Downloaded files: {self.downloaded_files}")
        print(f"Total uploaded bytes: {self.total_uploaded_bytes}")
        print(f"Total downloaded bytes: {self.total_downloaded_bytes}")
        print(f"Successful connections: {self.successful_connections}")
        print(f"Failed connections: {self.failed_connections}")
        print("-------------------------\n")

    def corrupt_chunk(self, chunk_index):
        """The power of corruption. Press C to corrupt a chunk of the terraria world."""
        if 0 <= chunk_index < len(self.chunks) and self.chunks[chunk_index] is not None:
            corrupted_data = bytearray(self.chunks[chunk_index])
            corrupted_data[0] = (corrupted_data[0] + 1) % 256
            self.chunks[chunk_index] = bytes(corrupted_data)
            print(f"Chunk {chunk_index} has been corrupted.")
        else:
            print(f"Invalid chunk index {chunk_index} or chunk is None.")

    def run(self):
        """Continues allowing peer to initiate outgoing connections."""
        while True:
            action = input("Enter action (u: upload, s: statistics, c: use the power of corruption, q: quit): ").lower()
            if action == 'u':
                target_ip = input("Enter the IP address of the peer to connect to: ")
                target_port = int(input("Enter the port of the peer to connect to: "))
                file_path = input("Enter the file path to upload: ")
                self.connect_to_peer(target_ip, target_port, file_path)
            elif action == 's':
                self.print_statistics()
            elif action == 'c':
                target_ip = input("Enter the IP address of the peer to connect to: ")
                target_port = int(input("Enter the port of the peer to connect to: "))
                file_path = input("Enter the file path to upload: ")
                self.connect_to_peer(target_ip, target_port, file_path)
                chunk_index = int(input("Choose the unlucky chunk to be corrupted: "))
                self.corrupt_chunk(chunk_index)
            elif action == 'q':
                print("Quitting...")
                break
            else:
                print("Invalid action. Please try again.")

# Ensure the program runs by adding the proper entry point below.
if __name__ == "__main__":
    peer_port = int(input("Enter the port for this peer: "))

    # Ensure 'received_files' directory exists to store downloaded files
    if not os.path.exists('received_files'):
        os.makedirs('received_files')

    peer_instance = Node(peer_port)
    peer_instance.run()