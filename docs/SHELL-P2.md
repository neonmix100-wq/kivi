# M8 P2: shell namespace, undo, thumbnails

## Shell namespace

The Files page has a **Компьютер** button. It opens `shell://` with:

- the workspace;
- Desktop, Documents, Downloads, Pictures, Music, Videos;
- local and removable drives (`C:`, `D:`, …);
- network drives when Windows exposes them (listed as `Сеть …`).

System directories are filtered from listings. File contents outside the
workspace are not readable by `fs.read`; the namespace is metadata-first.
Images/videos can still produce thumbnails.

## Undo

The **Отменить** button and `Ctrl+Z` undo the last operation kept in
`data/fs-undo.json` (up to 50):

| Operation | Undo |
|---|---|
| `fs.mkdir` | move the empty directory to the Recycle Bin |
| `fs.rename` | rename back |
| `fs.copy` | move the copy to the Recycle Bin |
| `fs.move` | move back |
| `fs.trash` | restore from a bounded local snapshot |

For Recycle Bin deletes Kivi keeps a local snapshot (up to 100 MB per item) in
`data/trash-undo/`, so undo works even though Windows owns the Recycle Bin.
Larger items are moved to the Recycle Bin without undo.

## Thumbnails

`GET /fs/thumb?path=...` returns a 320×220 JPEG:

- images via Pillow;
- the first frame of MP4/WebM/MKV/MOV via OpenCV.

Thumbnails load lazily in the file list.

## Search

Search is recursive and has no result-depth/result-count cap. It skips only
system/protected names, symlink cycles and unreadable directories. Searching a
whole drive can take time; searching the relevant folder is faster.

## Explorer and sorting

- Context menu has **Открыть в Проводнике** (`/fs/reveal`).
- Sort includes **По дате изменения**.
- Enter opens the focused row, Delete opens the trash form, F2 opens rename.

## Checks

```powershell
.venv\Scripts\python.exe -m pytest -q
```

```text
GET /fs/list?path=shell:// → known folders + drives
GET /fs/thumb?path=data/bench/ocr.png → image/jpeg
POST /fs/trash + POST /fs/undo → file restored
```
