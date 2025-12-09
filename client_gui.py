# client_gui.py
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
import socket, threading, json, os, time

SERVER_HOST = '127.0.0.1'
SERVER_PORT = 9009
RECV_BUFFER = 65536

class ChatClient:
    def __init__(self):
        self.sock = None
        self.running = False
        self.on_message = None
        self.on_users = None
        self._lock = threading.Lock()
        self._recv_buffer = b""   # for manual readline

    def connect(self, name, timeout=5.0):
        """
        Blocking connect/register: MUST be called from a background thread.
        """
        s = socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=timeout)
        # turn off any timeouts for steady recv
        s.settimeout(None)
        with self._lock:
            self.sock = s
            self._recv_buffer = b""

        # register
        self.send_json({"type": "register", "name": name})

        # wait for server response (manual readline)
        line = self._readline_blocking()
        if not line:
            raise RuntimeError("no response from server")
        try:
            resp = json.loads(line.decode())
        except Exception:
            raise RuntimeError("invalid response from server")
        if resp.get("type") == "error":
            raise RuntimeError(resp.get("msg", "registration error"))

        # start reader thread
        self.running = True
        threading.Thread(target=self.reader_thread, daemon=True).start()

    def close(self):
        self.running = False
        with self._lock:
            try:
                if self.sock:
                    try:
                        self.sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
                self._recv_buffer = b""
            except Exception:
                pass

    def send_json(self, obj):
        data = (json.dumps(obj) + "\n").encode()
        with self._lock:
            if not self.sock:
                raise RuntimeError("not connected")
            self.sock.sendall(data)

    def send_message(self, to, text):
        self.send_json({"type": "msg", "to": to, "text": text})

    def send_file(self, to, filepath):
        if not os.path.isfile(filepath):
            raise RuntimeError("file not found")
        size = os.path.getsize(filepath)
        filename = os.path.basename(filepath)
        # send header
        self.send_json({"type": "file", "to": to, "filename": filename, "size": size})
        # send raw bytes
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(RECV_BUFFER)
                if not chunk:
                    break
                with self._lock:
                    if not self.sock:
                        raise RuntimeError("connection lost during file send")
                    self.sock.sendall(chunk)

    def request_users(self):
        try:
            self.send_json({"type": "list"})
        except Exception:
            pass

    # -----------------------------
    # low-level helper: readline
    # -----------------------------
    def _readline_blocking(self):
        """Read from socket until a newline byte is found. Returns the line without newline."""
        while True:
            with self._lock:
                if not self.sock:
                    return b""
                # check buffer first
                if b"\n" in self._recv_buffer:
                    line, self._recv_buffer = self._recv_buffer.split(b"\n", 1)
                    return line
            # recv more (blocking)
            try:
                chunk = self.sock.recv(4096)
            except Exception as e:
                return b""
            if not chunk:
                return b""
            with self._lock:
                self._recv_buffer += chunk

    # -----------------------------
    # reader thread (background)
    # -----------------------------
    def reader_thread(self):
        try:
            while self.running:
                line = self._readline_blocking()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode())
                except Exception:
                    continue

                mtype = msg.get("type")
                if mtype == "msg":
                    frm = msg.get("from", "unknown")
                    text = msg.get("text", "")
                    if self.on_message:
                        GLib.idle_add(self.on_message, f"[{frm}] {text}")
                elif mtype == "users":
                    users = msg.get("users", [])
                    if self.on_users:
                        GLib.idle_add(self.on_users, users)
                elif mtype == "file":
                    # receive binary body after header
                    size = int(msg.get("size", 0))
                    sender = msg.get("from", "unknown")
                    fname = msg.get("filename", "file.bin")
                    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
                    os.makedirs(downloads, exist_ok=True)
                    path = os.path.join(downloads, f"from_{sender}_{fname}")

                    remaining = size
                    with open(path, "wb") as out:
                        while remaining > 0:
                            with self._lock:
                                if not self.sock:
                                    break
                                try:
                                    chunk = self.sock.recv(min(RECV_BUFFER, remaining))
                                except Exception:
                                    chunk = None
                            if not chunk:
                                break
                            out.write(chunk)
                            remaining -= len(chunk)

                    if remaining == 0:
                        GLib.idle_add(self.on_message, f"[file from {sender}] saved to {path}")
                    else:
                        GLib.idle_add(self.on_message, f"[file from {sender}] incomplete or failed")
                elif mtype == "ok":
                    GLib.idle_add(self.on_message, f"[system] {msg.get('msg', '')}")
                elif mtype == "error":
                    GLib.idle_add(self.on_message, f"[error] {msg.get('msg', '')}")
        except Exception as e:
            GLib.idle_add(self.on_message, f"[system] reader stopped: {e}")
        finally:
            self.running = False
            self.close()
            GLib.idle_add(self.on_message, "[system] disconnected")


class ChatWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="GTK Chat")
        self.set_default_size(700, 450)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add(vbox)

        # top: login controls
        hlogin = Gtk.Box(spacing=6)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_placeholder_text("Your name")
        self.connect_btn = Gtk.Button(label="Connect")
        self.connect_btn.connect("clicked", self.on_connect)

        hlogin.pack_start(self.name_entry, True, True, 0)
        hlogin.pack_start(self.connect_btn, False, False, 0)
        vbox.pack_start(hlogin, False, False, 0)

        # main area: messages and users
        hmain = Gtk.Box(spacing=6)
        vbox.pack_start(hmain, True, True, 0)

        # textview for messages
        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.buffer = self.textview.get_buffer()
        sc = Gtk.ScrolledWindow()
        sc.set_hexpand(True); sc.set_vexpand(True)
        sc.add(self.textview)
        hmain.pack_start(sc, True, True, 0)

        # user list (TreeView) - use model= to avoid deprecation warning
        self.user_list_store = Gtk.ListStore(str)
        self.user_tree = Gtk.TreeView(model=self.user_list_store)
        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("Users", renderer, text=0)
        self.user_tree.append_column(column)
        self.user_tree.set_size_request(160, -1)

        side_sc = Gtk.ScrolledWindow()
        side_sc.set_size_request(160, 300)
        side_sc.add(self.user_tree)
        hmain.pack_start(side_sc, False, False, 0)

        # bottom: message entry + send + file
        hbot = Gtk.Box(spacing=6)
        self.msg_entry = Gtk.Entry()
        self.send_btn = Gtk.Button(label="Send")
        self.send_btn.connect("clicked", self.on_send)
        self.file_btn = Gtk.Button(label="Send File")
        self.file_btn.connect("clicked", self.on_file)

        hbot.pack_start(self.msg_entry, True, True, 0)
        hbot.pack_start(self.send_btn, False, False, 0)
        hbot.pack_start(self.file_btn, False, False, 0)
        vbox.pack_start(hbot, False, False, 0)

        # client instance
        self.client = ChatClient()
        self.client.on_message = self.append_text
        self.client.on_users = self.update_users
        self.connected = False

        self.connect("destroy", self.on_destroy)

    def append_text(self, text):
        end = self.buffer.get_end_iter()
        self.buffer.insert(end, text + "\n")
        # auto scroll to end
        mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), True)
        view = self.textview
        view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def update_users(self, users):
        self.user_list_store.clear()
        for u in users:
            self.user_list_store.append([u])

    def on_connect(self, widget):
        if self.connected:
            self.append_text("[system] already connected")
            return

        name = self.name_entry.get_text().strip()
        if not name:
            self.append_text("[system] enter name")
            return

        def connector():
            try:
                self.client.connect(name)
                GLib.idle_add(self._on_connected_ok)
            except Exception as e:
                GLib.idle_add(self.append_text, f"[error] {e}")

        threading.Thread(target=connector, daemon=True).start()
        self.append_text("[system] connecting...")

    def _on_connected_ok(self):
        self.connected = True
        self.append_text("[system] connected")
        try:
            self.client.request_users()
        except Exception:
            pass

    def get_selected_user(self):
        selection = self.user_tree.get_selection()
        model, iter_ = selection.get_selected()
        if iter_:
            return model[iter_][0]
        return None

    def on_send(self, widget):
        if not self.connected:
            self.append_text("[system] not connected")
            return
        to = self.get_selected_user()
        text = self.msg_entry.get_text()
        if not text:
            return
        try:
            self.client.send_message(to, text)
            if to:
                self.append_text(f"[me → {to}] {text}")
            else:
                self.append_text(f"[me] {text}")
            self.msg_entry.set_text("")
        except Exception as e:
            self.append_text(f"[error] {e}")

    def on_file(self, widget):
        if not self.connected:
            self.append_text("[system] not connected")
            return

        dialog = Gtk.FileChooserDialog("Choose file", self, Gtk.FileChooserAction.OPEN,
                                       (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            filepath = dialog.get_filename()
            to = self.get_selected_user()
            if not to:
                self.append_text("[system] select user first for file transfer")
            else:
                self.append_text(f"[system] sending file {filepath} to {to}")
                def file_sender():
                    try:
                        self.client.send_file(to, filepath)
                        GLib.idle_add(self.append_text, f"[system] file sent: {os.path.basename(filepath)}")
                    except Exception as e:
                        GLib.idle_add(self.append_text, f"[error] file send failed: {e}")
                threading.Thread(target=file_sender, daemon=True).start()
        dialog.destroy()

    def on_destroy(self, widget):
        try:
            self.client.close()
        except Exception:
            pass
        Gtk.main_quit()

def main():
    win = ChatWindow()
    win.show_all()
    Gtk.main()

if __name__ == "__main__":
    main()
