import io
import os
import pathlib
import re
import subprocess
import sys
import zipfile
import pytest
from app.ui import web_app

def test_zip_creation_with_utf8_and_dirs(tmp_path: pathlib.Path) -> None:
    subj_dir = tmp_path / "資訊處理" / "113年"
    subj_dir.mkdir(parents=True)
    pdf1 = subj_dir / "資料結構_申論試題.pdf"
    pdf1.write_bytes(b"%PDF-1.4 dummy valid content")

    # 建立無效 0 位元組檔案，應被略過
    pdf_zero = subj_dir / "空檔案.pdf"
    pdf_zero.write_bytes(b"")

    data, count = web_app._create_zip_from_paths(str(tmp_path), (pdf1.relative_to(tmp_path).as_posix(),))
    assert count == 1
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

    data, count = web_app._create_zip_from_paths(str(tmp_path), (long_file.relative_to(tmp_path).as_posix(),))
    assert count == 1
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

def test_cloud_root_name_uses_timestamp() -> None:
    assert web_app.cloud_root_name("20261103_2125") == "國考題庫_20261103_2125"
    assert re.fullmatch(r"國考題庫_\d{8}_\d{4}", web_app.cloud_root_name())


def test_new_cloud_root_is_timestamped_temp_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(web_app.tempfile, "tempdir", str(tmp_path))
    root = pathlib.Path(web_app.new_cloud_root())
    assert root.parent == tmp_path
    assert re.fullmatch(r"國考題庫_\d{8}_\d{4}", root.name)


def test_cloud_zip_name_matches_root_folder(monkeypatch) -> None:
    monkeypatch.setattr(web_app, "is_cloud_env", lambda: True)
    root = str(pathlib.Path("/tmp") / "國考題庫_20261103_2125")
    assert web_app.zip_file_name(root, "task-abc") == "國考題庫_20261103_2125.zip"


def test_local_zip_name_keeps_task_id(monkeypatch) -> None:
    monkeypatch.setattr(web_app, "is_cloud_env", lambda: False)
    name = web_app.zip_file_name(r"C:\國考題庫", "task-abc")
    assert re.fullmatch(r"國考題庫_task-abc_\d{8}_\d{6}\.zip", name)


def test_button_label_depends_on_cloud(monkeypatch) -> None:
    monkeypatch.setenv("STREAMLIT_SHARING_HOST", "share.streamlit.io")
    assert web_app.is_cloud_env() is True
    monkeypatch.delenv("STREAMLIT_SHARING_HOST", raising=False)


def test_zip_only_includes_task_files_and_deduplicates(tmp_path):
    (tmp_path / "歷史.pdf").write_bytes(b"old")
    (tmp_path / "本次.pdf").write_bytes(b"current")
    data, count = web_app._create_zip_from_paths(str(tmp_path), ("本次.pdf", "./本次.pdf"))
    assert count == 1
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == ["本次.pdf"]
        assert zf.read("本次.pdf") == b"current"


@pytest.mark.parametrize("bad_path", ["不存在.pdf", "空.pdf", "../外部.pdf", "_系統資料/內部.pdf"])
def test_invalid_export_stops_entire_archive(tmp_path, bad_path):
    (tmp_path / "有效.pdf").write_bytes(b"valid")
    (tmp_path / "空.pdf").touch()
    with pytest.raises(ValueError):
        web_app._create_zip_from_paths(str(tmp_path), ("有效.pdf", bad_path))


def test_absolute_path_rejected(tmp_path):
    with pytest.raises(ValueError, match="超出"):
        web_app._create_zip_from_paths(str(tmp_path), (str(tmp_path / "absolute.pdf"),))


def test_empty_export(tmp_path):
    assert web_app._create_zip_from_paths(str(tmp_path), ()) == (None, 0)
