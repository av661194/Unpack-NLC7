#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xr_db_lz4_unpack_gui.py - GUI-распаковщик S.T.A.L.K.E.R. (X-Ray) архивов .db0/.db1/...
Формат: чанки + таблица файлов, данные LZ4F (нестандартная обёртка).

Зависимости: только стандартная библиотека (Tkinter).
Опционально: pip install lz4 - для быстрой распаковки.
"""
import argparse, fnmatch, os, struct, sys, threading, queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import lz4.block as _lz4
except Exception:
    _lz4 = None


def lz4_block_decompress(src, size):
    if _lz4 is not None:
        return _lz4.decompress(src, uncompressed_size=size)
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]; i += 1
        ll = tok >> 4
        if ll == 15:
            while True:
                b = src[i]; i += 1; ll += b
                if b != 255: break
        out += src[i:i + ll]; i += ll
        if i >= n: break
        off = src[i] | (src[i + 1] << 8); i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]; i += 1; ml += b
                if b != 255: break
        ml += 4
        if off == 0 or off > len(out):
            raise ValueError("bad LZ4 offset")
        st = len(out) - off
        if off >= ml:
            out += out[st:st + ml]
        else:
            for k in range(ml):
                out.append(out[st + k])
    if len(out) != size:
        raise ValueError("size mismatch %d != %d" % (len(out), size))
    return bytes(out)


def unwrap(chunk):
    if chunk[:4] == b"LZ4F":
        real = struct.unpack_from("<I", chunk, 4)[0]
        return lz4_block_decompress(chunk[9:], real)
    return chunk


def read_chunks(db):
    p, res = 0, []
    while p + 8 <= len(db):
        cid, sz = struct.unpack_from("<II", db, p)
        if p + 8 + sz > len(db):
            res.append((cid, p + 8, len(db) - p - 8, True))
            break
        res.append((cid, p + 8, sz, False))
        p += 8 + sz
    return res


def parse_table(t):
    ents, p = [], 0
    while p + 4 <= len(t):
        size, flags = struct.unpack_from("<HH", t, p)
        if flags == 0x81:
            body = size
            if p + 2 + body > len(t): break
            ents.append(dict(dir=True, name=t[p + 24:p + 2 + body]))
        else:
            body = size + 4
            if p + 2 + body > len(t) or body < 26: break
            real, comp, h, mt, _pad, off = struct.unpack_from("<6I", t, p + 4)
            ents.append(dict(dir=False, name=t[p + 28:p + 2 + body], real=real,
                             comp=comp, hash=h, mtime=mt, off=off, flags=flags))
        p += 2 + body
    return ents, p, len(t)


def load_archive(path):
    with open(path, "rb") as f:
        db = f.read()
    chunks = read_chunks(db)
    table = None
    for cid, off, sz, trunc in chunks:
        if (cid & 0x7FFFFFFF) == 1 and table is None:
            table = unwrap(db[off:off + sz]) if cid & 0x80000000 else db[off:off + sz]
            # Получение дампа
            # open("dbb_table.bin", "wb").write(table)
    if table is None:
        raise ValueError("Таблица файлов (чанк 1) не найдена")
    ents, used, total = parse_table(table)
    warn = None
    if used != total:
        warn = "Таблица разобрана на %d из %d байт" % (used, total)
    return db, [e for e in ents if not e["dir"]], warn


def get_data(db, e):
    d = db[e["off"]:e["off"] + e["comp"]]
    if len(d) != e["comp"]:
        raise ValueError("данные выходят за конец файла")
    r = unwrap(d)
    if len(r) != e["real"]:
        raise ValueError("размер %d != %d" % (len(r), e["real"]))
    return r


def safe_join(base, name):
    rel = name.replace("\\", "/").lstrip("/")
    parts = [x for x in rel.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise ValueError("небезопасный путь")
    return os.path.join(base, *parts)


def dname(e):
    try: return e["name"].decode("cp1251")
    except Exception: return e["name"].decode("latin-1")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("X-Ray DB LZ4 Unpacker")
        self.geometry("900x620")
        self.db = None
        self.files = []
        self.archive_path = ""
        self.out_dir = ""
        self.q = queue.Queue()
        self.worker = None
        self._build()

    def _build(self):
        top = ttk.Frame(self, padding=8); top.pack(fill="x")
        ttk.Label(top, text="Архив:").grid(row=0, column=0, sticky="w")
        self.var_archive = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_archive).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Открыть...", command=self.pick_archive).grid(row=0, column=2)
        ttk.Label(top, text="Папка вывода:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.var_out = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_out).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(top, text="Выбрать...", command=self.pick_out).grid(row=1, column=2, pady=(6, 0))
        top.columnconfigure(1, weight=1)

        filt = ttk.Frame(self, padding=(8, 0)); filt.pack(fill="x")
        ttk.Label(filt, text="Фильтр:").pack(side="left")
        self.var_filter = tk.StringVar()
        ttk.Entry(filt, textvariable=self.var_filter).pack(side="left", fill="x", expand=True, padx=6)
        self.var_filter.trace_add("write", lambda *_: self.refresh_list())
        ttk.Label(filt, text="например: config\\*.ltx").pack(side="left")

        mid = ttk.Frame(self, padding=8); mid.pack(fill="both", expand=True)
        cols = ("real", "comp", "name")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings")
        self.tree.heading("real", text="Размер")
        self.tree.heading("comp", text="Сжатый")
        self.tree.heading("name", text="Файл")
        self.tree.column("real", width=110, anchor="e")
        self.tree.column("comp", width=110, anchor="e")
        self.tree.column("name", width=560, anchor="w")
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        btns = ttk.Frame(self, padding=8); btns.pack(fill="x")
        self.btn_list = ttk.Button(btns, text="Открыть архив", command=self.open_archive)
        self.btn_list.pack(side="left")
        self.btn_extract = ttk.Button(btns, text="Распаковать выбранное", command=self.extract_selected, state="disabled")
        self.btn_extract.pack(side="left", padx=6)
        self.btn_extract_all = ttk.Button(btns, text="Распаковать всё", command=self.extract_all, state="disabled")
        self.btn_extract_all.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="Стоп", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=8)
        self.status = tk.StringVar(value="Готово. Откройте .db0/.db1 архив.")
        ttk.Label(self, textvariable=self.status, padding=(8, 4), anchor="w").pack(fill="x")
        self.log = tk.Text(self, height=8, state="disabled")
        self.log.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self.after(100, self._poll)

    def log_write(self, s):
        self.log.configure(state="normal")
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def pick_archive(self):
        p = filedialog.askopenfilename(title="Выберите архив X-Ray",
            filetypes=[("X-Ray archives", "*.xdb* *.db*"), ("All files", "*.*")])
        if p:
            self.var_archive.set(p)
            self.open_archive()

    def pick_out(self):
        p = filedialog.askdirectory(title="Выберите папку для распаковки")
        if p:
            self.var_out.set(p)

    def open_archive(self):
        path = self.var_archive.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("Ошибка", "Укажите существующий файл архива.")
            return
        self.status.set("Чтение архива...")
        self.update_idletasks()
        try:
            self.db, self.files, warn = load_archive(path)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            self.status.set("Ошибка: %s" % e)
            return
        self.archive_path = path
        if not self.var_out.get():
            base = os.path.splitext(os.path.basename(path))[0]
            self.var_out.set(os.path.join(os.path.dirname(path), base + "_unpacked"))
        self.refresh_list()
        self.btn_extract.configure(state="normal")
        self.btn_extract_all.configure(state="normal")
        msg = "Загружено файлов: %d" % len(self.files)
        if warn: msg += " | " + warn
        if _lz4 is None: msg += " | lz4 не установлен, используется медленный Python-декодер"
        self.status.set(msg)
        self.log_write("Открыт: %s (%d файлов)" % (path, len(self.files)))

    def selected(self):
        flt = self.var_filter.get().strip().replace("/", "\\").lower()
        if not flt:
            return self.files
        return [e for e in self.files if fnmatch.fnmatch(dname(e).lower(), flt)]

    def refresh_list(self):
        self.tree.delete(*self.tree.get_children())
        for e in self.selected():
            self.tree.insert("", "end", values=(e["real"], e["comp"], dname(e)))

    def extract_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Нет выбора", "Выберите файлы в списке.")
            return
        idx = [self.tree.index(i) for i in sel]
        files = [self.selected()[i] for i in idx]
        self.start_extract(files)

    def extract_all(self):
        self.start_extract(self.selected())

    def start_extract(self, files):
        out = self.var_out.get().strip()
        if not out:
            messagebox.showerror("Ошибка", "Укажите папку вывода.")
            return
        if not files:
            messagebox.showinfo("Пусто", "Нет файлов для распаковки.")
            return
        self.btn_extract.configure(state="disabled")
        self.btn_extract_all.configure(state="disabled")
        self.btn_list.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress.configure(maximum=len(files), value=0)
        self.status.set("Распаковка: 0/%d" % len(files))
        self.stop_flag = False
        self.worker = threading.Thread(target=self._work, args=(files, out), daemon=True)
        self.worker.start()

    def _work(self, files, out):
        ok = bad = 0
        for i, e in enumerate(files, 1):
            if self.stop_flag:
                self.q.put(("status", "Остановлено пользователем"))
                break
            n = dname(e)
            try:
                data = get_data(self.db, e)
                dst = safe_join(out, n)
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(data)
                ok += 1
            except Exception as ex:
                bad += 1
                self.q.put(("log", "ОШИБКА %s: %s" % (n, ex)))
            self.q.put(("progress", i))
            if i % 100 == 0:
                self.q.put(("status", "Распаковка: %d/%d" % (i, len(files))))
        self.q.put(("done", (ok, bad, out)))

    def stop(self):
        self.stop_flag = True

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "progress":
                    self.progress.configure(value=val)
                elif kind == "status":
                    self.status.set(val)
                elif kind == "log":
                    self.log_write(val)
                elif kind == "done":
                    ok, bad, out = val
                    self.status.set("Готово: %d ок, %d ошибок -> %s" % (ok, bad, out))
                    self.log_write("Готово: %d ок, %d ошибок -> %s" % (ok, bad, out))
                    self.btn_extract.configure(state="normal")
                    self.btn_extract_all.configure(state="normal")
                    self.btn_list.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll)


def main():
    # Поддержка CLI: если переданы аргументы, работаем как консольная утилита
    if len(sys.argv) > 1:
        ap = argparse.ArgumentParser()
        ap.add_argument("cmd", choices=("list", "extract"))
        ap.add_argument("archive")
        ap.add_argument("-o", "--out", default=None)
        ap.add_argument("-f", "--filter", default=None)
        a = ap.parse_args()
        db, files, warn = load_archive(a.archive)
        pat = a.filter.replace("/", "\\").lower() if a.filter else None
        sel = [e for e in files if not pat or fnmatch.fnmatch(dname(e).lower(), pat)]
        if a.cmd == "list":
            for e in sel:
                print("%10d %10d  %s" % (e["real"], e["comp"], dname(e)))
            print("файлов: %d" % len(sel))
            return
        out = a.out or (os.path.splitext(os.path.basename(a.archive))[0] + "_unpacked")
        ok = bad = 0
        for e in sel:
            try:
                data = get_data(db, e)
                dst = safe_join(out, dname(e))
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                with open(dst, "wb") as f: f.write(data)
                ok += 1
            except Exception as ex:
                bad += 1; print("ОШИБКА %s: %s" % (dname(e), ex), file=sys.stderr)
        print("готово: %d ок, %d ошибок -> %s" % (ok, bad, out))
        return
    App().mainloop()


if __name__ == "__main__":
    main()
