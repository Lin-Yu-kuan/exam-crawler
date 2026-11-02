import io
import pathlib
import zipfile
from app.ui import web_app

def test_zip_creation_with_utf8_flag(tmp_path: pathlib.Path) -> None:
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
        assert len(infolist) == 1
        zinfo = infolist[0]
        # 驗證 UTF-8 旗標 0x800 是否開啟
        assert zinfo.flag_bits & 0x800 != 0
        assert zinfo.filename == "資訊處理/113年/資料結構_申論試題.pdf"
        assert zf.read(zinfo.filename) == b"%PDF-1.4 dummy valid content"

def test_button_label_depends_on_cloud(monkeypatch) -> None:
    monkeypatch.setenv("STREAMLIT_SHARING_HOST", "share.streamlit.io")
    assert web_app.is_cloud_env() is True
    
    # 測試雲端標籤為「開始加載」
    monkeypatch.delenv("STREAMLIT_SHARING_HOST", raising=False)
