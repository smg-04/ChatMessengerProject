# server.py
# Python3 asyncio-based chat+file relay server
import asyncio
import json
from typing import Dict, Tuple

HOST = '0.0.0.0'
PORT = 9009

# client_name -> (reader, writer)
clients: Dict[str, Tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
lock = asyncio.Lock()

async def send_json(writer: asyncio.StreamWriter, obj):
    data = (json.dumps(obj) + "\n").encode()
    writer.write(data)
    try:
        await writer.drain()
    except Exception:
        pass

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info('peername')
    name = None
    try:
        # Require registration as first step (one JSON line)
        line = await reader.readline()
        if not line:
            writer.close(); await writer.wait_closed(); return
        try:
            msg = json.loads(line.decode())
        except Exception:
            await send_json(writer, {"type":"error","msg":"invalid register json"})
            writer.close(); await writer.wait_closed(); return

        if msg.get("type") != "register" or not msg.get("name"):
            await send_json(writer, {"type":"error","msg":"register first with {'type':'register','name':'yourname'}"})
            writer.close(); await writer.wait_closed(); return

        name = msg["name"]
        async with lock:
            if name in clients:
                await send_json(writer, {"type":"error","msg":"name already taken"})
                writer.close(); await writer.wait_closed(); return
            clients[name] = (reader, writer)
            print(f"{name} registered from {addr}")
            # notify all
            await broadcast_users()

        await send_json(writer, {"type":"ok","msg":"registered"})

        # main loop: read header lines (JSON per line)
        while True:
            header_line = await reader.readline()
            if not header_line:
                break
            try:
                header = json.loads(header_line.decode())
            except Exception:
                await send_json(writer, {"type":"error","msg":"invalid json header"})
                continue

            htype = header.get("type")
            if htype == "msg":
                to = header.get("to")  # None for broadcast
                text = header.get("text","")
                if to:
                    # direct message
                    async with lock:
                        if to in clients:
                            _, w = clients[to]
                            await send_json(w, {"type":"msg","from":name,"text":text})
                        else:
                            await send_json(writer, {"type":"error","msg":"user not online"})
                else:
                    # broadcast
                    await broadcast({"type":"msg","from":name,"text":text})
            elif htype == "file":
                to = header.get("to")
                filename = header.get("filename")
                size = int(header.get("size",0))
                if not to or to not in clients:
                    await send_json(writer, {"type":"error","msg":"recipient not found"})
                    # consume and discard body safely
                    try:
                        await reader.readexactly(size)
                    except Exception:
                        pass
                    continue
                # forward header to recipient
                _, w = clients[to]
                await send_json(w, {"type":"file","from":name,"filename":filename,"size":size})
                # relay binary data
                remaining = size
                try:
                    while remaining > 0:
                        chunk = await reader.read(min(65536, remaining))
                        if not chunk:
                            break
                        w.write(chunk)
                        try:
                            await w.drain()
                        except Exception:
                            pass
                        remaining -= len(chunk)
                except Exception:
                    pass
                print(f"relayed file {filename} from {name} to {to}")
            elif htype == "list":
                await send_user_list(writer)
            else:
                await send_json(writer, {"type":"error","msg":"unknown type"})
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print("client error:", e)
    finally:
        if name:
            async with lock:
                if name in clients:
                    del clients[name]
                    print(f"{name} disconnected")
                    await broadcast_users()
        try:
            writer.close()
            await writer.wait_closed()
        except:
            pass

async def broadcast(obj):
    async with lock:
        to_remove = []
        for n,(r,w) in clients.items():
            try:
                await send_json(w, obj)
            except Exception:
                to_remove.append(n)
        for n in to_remove:
            if n in clients:
                del clients[n]

async def send_user_list(writer):
    async with lock:
        names = list(clients.keys())
    await send_json(writer, {"type":"users","users":names})

async def broadcast_users():
    async with lock:
        names = list(clients.keys())
    await broadcast({"type":"users","users":names})

async def main():
    server = await asyncio.start_server(handle_client, HOST, PORT)
    addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets)
    print(f"Serving on {addrs}")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Server shutting down")
