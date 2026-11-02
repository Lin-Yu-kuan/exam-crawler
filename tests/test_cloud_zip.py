import io
import os
import pathlib
import subprocess
import sys
import zipfile
from app.ui import web_app

def test_zip_creation_with_utf8_and_dirs(tmp_path: pathlib.Path) -> None:
    subj_dir = tmp_path / "資訊處理" / "113年"
    subj_dir.mkdir(parents=True)
    pdf1 = subj_dir / "資料結構_申論試題.pdf"
    pdf1.write_bytes(b"%PDF-1.4 dummy valid content")

    # 建立無效 0 位元組檔案，應被略過
    pdf_zero = subj_dir / "空檔案.pdf"
    pdf_zero.write_bytes(b"")

    data = web_app._create_zip_from_root(str(tmp_path))
    assert data is not None
    assert len(data) > 22

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infolist = zf.infolist()
        dir_entries = [i for i in infolist if i.filename.endswith("/")]
        file_entries = [i for i in infolist if not i.filename.endswith("/")]
        
        assert len(dir_entries) == 2
        assert len(file_entries) == 1
        
        for d in dir_entries:
            assert d.external_attr == 0x10
            assert d.create_system == 0
            assert d.flag_bits & 0x800 != 0
            
        zinfo = file_entries[0]
        assert zinfo.flag_bits & 0x800 != 0
        assert zinfo.create_system == 0
        assert zinfo.external_attr == 0x20
        assert zinfo.filename == "資訊處理/113年/資料結構_申論試題.pdf"
        assert zf.read(zinfo.filename) == b"%PDF-1.4 dummy valid content"

def test_zip_creation_shortens_ultra_long_path(tmp_path: pathlib.Path) -> None:
    # 模擬考選部超長合併考試名稱（超過 260 UTF-8 bytes）
    long_exam = "113年專門職業及技術人員高等考試建築師、32類科技師(含第二次食品技師)、大地工程技師考試分階段考試(第二階段考試)暨普通考試不動產經紀人、記帳士考試"
    long_dir = tmp_path / "資料結構與資料庫及資料探勘"
    long_dir.mkdir(parents=True)
    long_file = long_dir / f"113_{long_exam}_專技高考_資訊技師_試題.pdf"
    long_file.write_bytes(b"%PDF-1.4 long path test")

    data = web_app._create_zip_from_root(str(tmp_path))
    assert data is not None

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        file_entries = [i for i in zf.infolist() if not i.filename.endswith("/")]
        assert len(file_entries) == 1
        fn = file_entries[0].filename
        assert len(fn.encode("utf-8")) <= 180
        assert fn.endswith("_試題.pdf")
        assert zf.read(fn) == b"%PDF-1.4 long path test"

    # 若在 Windows 上，透過 Shell.Application 驗證 Windows 檔案總管可以正常識別
    if sys.platform == "win32":
        test_zip_path = tmp_path / "win_test.zip"
        test_zip_path.write_bytes(data)
        ps_script = f"""
        $sh = New-Object -ComObject Shell.Application
        $z = $sh.NameSpace('{str(test_zip_path)}')
        if ($null -eq $z) {{ exit 1 }}
        if ($z.Items().Count -gt 0) {{ exit 0 }} else {{ exit 2 }}
        """
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], capture_output=True)
        assert r.returncode == 0, f"Windows Shell.Application failed: {r.stderr.decode('utf-8', errors='ignore')}"

def test_button_label_depends_on_cloud(monkeypatch) -> None:
    monkeypatch.setenv("STREAMLIT_SHARING_HOST", "share.streamlit.io")
    assert web_app.is_cloud_env() is True
    monkeypatch.delenv("STREAMLIT_SHARING_HOST", raising=False)
