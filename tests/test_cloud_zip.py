import io
import pathlib
import zipfile
from app.ui import web_app

def test_zip_creation_from_root(tmp_path: pathlib.Path) -> None:
    # 建立階層目錄與測試 PDF
    subj_dir = tmp_path / "資訊處理" / "113年"
    subj_dir.mkdir(parents=True)
    pdf1 = subj_dir / "資料結構_申論試題.pdf"
    pdf1.write_bytes(b"%PDF-1.4 dummy content 1")
    
    # 建立系統內部目錄（不應被打包）
    sys_dir = tmp_path / web_app.SYSTEM_DIR_NAME
    sys_dir.mkdir(parents=True)
    (sys_dir / "manifest.db").write_bytes(b"dummy db")
    
    parts_dir = tmp_path / ".parts"
    parts_dir.mkdir(parents=True)
    (parts_dir / "temp.part").write_bytes(b"dummy part")

    data = web_app._create_zip_from_root(str(tmp_path))
    assert data is not None
    assert len(data) > 0

    # 驗證 ZIP 內容
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        namelist = zf.namelist()
        assert len(namelist) == 1
        assert "資訊處理/113年/資料結構_申論試題.pdf" in [n.replace("\\", "/") for n in namelist]
        assert zf.read(namelist[0]) == b"%PDF-1.4 dummy content 1"

def test_is_cloud_env_detection(monkeypatch) -> None:
    # 預設 Windows 本地
    assert web_app.is_cloud_env() is False or not web_app.sys.platform.startswith("win")
    
    # 模擬 Streamlit Cloud 環境變數
    monkeypatch.setenv("STREAMLIT_SHARING_HOST", "share.streamlit.io")
    assert web_app.is_cloud_env() is True

