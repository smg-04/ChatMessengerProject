# server.py (Fixed Deadlock Version)
import asyncio
import json
from typing import Dict, Tuple

HOST = '0.0.0.0'
PORT = 9009

# client_name -> (reader, writer)
clients: Dict[str, Tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
lock = asyncio.Lock()

async def send_json(writer: asyncio.StreamWriter, obj):
    try:
        data = (json.dumps(obj) + "\n").encode()
        writer.write(data)
        await writer.drain()
    except Exception:
        pass

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info('peername')
    name = None
    try:
        # Require registration as first step
        line = await reader.readline()
        if not line:
            writer.close(); await writer.wait_closed(); return
        try:
            msg = json.loads(line.decode())
        except Exception:
            await send_json(writer, {"type":"error","msg":"invalid register json"})
            writer.close(); await writer.wait_closed(); return

        if msg.get("type") != "register" or not msg.get("name"):
            await send_json(writer, {"type":"error","msg":"register first"})
            writer.close(); await writer.wait_closed(); return

        name = msg["name"]

        # ▼▼▼ [중요 수정] Lock 범위를 최소화하여 교착 상태 방지 ▼▼▼
        async with lock:
            if name in clients:
                await send_json(writer, {"type":"error","msg":"name already taken"})
                writer.close(); await writer.wait_closed(); return
            clients[name] = (reader, writer)
            print(f"{name} registered from {addr}")
        # ▲▲▲ Lock이 여기서 풀립니다 ▲▲▲

        # Lock이 풀린 상태에서 방송(Broadcast)을 해야 안전함
        await broadcast_users()
        await send_json(writer, {"type":"ok","msg":"registered"})

        # main loop
        while True:
            header_line = await reader.readline()
            if not header_line:
                break
            try:
                header = json.loads(header_line.decode())
            except Exception:
                continue

            htype = header.get("type")
            if htype == "msg":
                to = header.get("to")
                text = header.get("text","")
                if to:
                    # direct message
                    target_writer = None
                    async with lock:
                        if to in clients:
                            _, target_writer = clients[to]
                    
                    if target_writer:
                        await send_json(target_writer, {"type":"msg","from":name,"text":text})
                    else:
                        await send_json(writer, {"type":"error","msg":"user not online"})
                else:
                    # broadcast
                    await broadcast({"type":"msg","from":name,"text":text})

            elif htype == "file":
                to = header.get("to")
                filename = header.get("filename")
                size = int(header.get("size",0))

                target_writer = None
                async with lock:
                    if to in clients:
                        _, target_writer = clients[to]
                
                if not target_writer:
                    await send_json(writer, {"type":"error","msg":"recipient not found"})
                    # consume body to avoid sync issues
                    try:
                        await reader.readexactly(size)
                    except: pass
                    continue
                
                # forward header
                await send_json(target_writer, {"type":"file","from":name,"filename":filename,"size":size})
                
                # relay binary data
                remaining = size
                try:
                    while remaining > 0:
                        chunk = await reader.read(min(65536, remaining))
                        if not chunk: break
                        target_writer.write(chunk)
                        await target_writer.drain()
                        remaining -= len(chunk)
                except: pass
                print(f"relayed file {filename} from {name} to {to}")

            elif htype == "list":
                await send_user_list(writer)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"client error ({name}):", e)
    finally:
        if name:
            async with lock:
                if name in clients:
                    del clients[name]
                    print(f"{name} disconnected")
            # Lock 풀고 방송
            await broadcast_users()
        
        try:
            writer.close()
            await writer.wait_closed()
        except: pass

async def broadcast(obj):
    # 안전하게 보낼 목록을 먼저 복사
    async with lock:
        active_writers = [w for (r, w) in clients.values()]
    
    for w in active_writers:
        try:
            await send_json(w, obj)
        except: pass

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