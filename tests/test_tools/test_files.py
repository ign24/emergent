"""Tests for file tools."""

from __future__ import annotations

import pytest

from emergent import SafetyViolationError
from emergent.tools.files import (
    directory_tree,
    file_delete,
    file_info,
    file_move,
    file_read,
    file_write,
    list_directory,
    search_files,
    search_in_files,
)


class TestFileRead:
    async def test_read_existing_file(self, tmp_path, monkeypatch):
        # Create a test file in tmp_path and monkeypatch SANDBOX_ROOT
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        result = await file_read({"path": "test.txt"})
        assert "hello world" in result

    async def test_file_not_found(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        result = await file_read({"path": "nonexistent.txt"})
        assert "not found" in result.lower() or "FILE_NOT_FOUND" in result

    async def test_path_traversal_blocked(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        with pytest.raises(SafetyViolationError, match="PATH_TRAVERSAL|OUTSIDE_SANDBOX"):
            await file_read({"path": "../../etc/passwd"})

    async def test_sensitive_env_blocked(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        # Create a .env file
        (tmp_path / ".env").write_text("SECRET=value")

        with pytest.raises(SafetyViolationError, match="SENSITIVE_PATH"):
            await file_read({"path": ".env"})

    async def test_ssh_key_blocked(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "id_rsa").write_text("PRIVATE KEY")

        with pytest.raises(SafetyViolationError, match="SENSITIVE_PATH"):
            await file_read({"path": ".ssh/id_rsa"})


class TestFileWrite:
    async def test_create_new_file(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        result = await file_write({"path": "new_file.txt", "content": "hello", "mode": "create"})
        assert "created" in result.lower()
        assert (tmp_path / "new_file.txt").read_text() == "hello"

    async def test_create_fails_if_exists(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "existing.txt").write_text("old")
        result = await file_write({"path": "existing.txt", "content": "new", "mode": "create"})
        assert "already exists" in result.lower()

    async def test_overwrite_mode(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "file.txt").write_text("old content")
        result = await file_write(
            {"path": "file.txt", "content": "new content", "mode": "overwrite"}
        )
        assert "overwritten" in result.lower()
        assert (tmp_path / "file.txt").read_text() == "new content"

    async def test_append_mode(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "file.txt").write_text("line1\n")
        result = await file_write({"path": "file.txt", "content": "line2\n", "mode": "append"})
        assert "appended" in result.lower()
        assert (tmp_path / "file.txt").read_text() == "line1\nline2\n"

    async def test_path_traversal_blocked(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        with pytest.raises(SafetyViolationError):
            await file_write({"path": "../../etc/evil.txt", "content": "evil"})


# ---------------------------------------------------------------------------
# list_directory
# ---------------------------------------------------------------------------


class TestListDirectory:
    async def test_list_files_and_dirs(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "subdir").mkdir()
        (tmp_path / "file.txt").write_text("hello")

        result = await list_directory({"path": "."})
        assert "[DIR]  subdir/" in result
        assert "[FILE] file.txt" in result

    async def test_empty_directory(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        empty = tmp_path / "empty"
        empty.mkdir()

        result = await list_directory({"path": "empty"})
        assert "empty" in result.lower()

    async def test_hidden_files_excluded_by_default(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / ".hidden").write_text("secret")
        (tmp_path / "visible.txt").write_text("public")

        result = await list_directory({"path": "."})
        assert ".hidden" not in result
        assert "visible.txt" in result

    async def test_hidden_files_shown_when_requested(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / ".hidden").write_text("secret")

        result = await list_directory({"path": ".", "show_hidden": True})
        assert ".hidden" in result

    async def test_path_traversal_blocked(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        with pytest.raises(SafetyViolationError):
            await list_directory({"path": "../../etc"})


# ---------------------------------------------------------------------------
# directory_tree
# ---------------------------------------------------------------------------


class TestDirectoryTree:
    async def test_basic_tree(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b.txt").write_text("content")
        (tmp_path / "c.txt").write_text("content")

        result = await directory_tree({"path": "."})
        assert "a/" in result
        assert "b.txt" in result
        assert "c.txt" in result

    async def test_depth_limit(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        # Create nested: a/b/c/d.txt
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "d.txt").write_text("deep")

        result = await directory_tree({"path": ".", "max_depth": 1})
        assert "a/" in result
        # b should not appear at depth 1 — a is at depth 1, b would be depth 2
        assert "b/" not in result

    async def test_max_entries_truncation(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)
        monkeypatch.setattr(files_module, "_MAX_TREE_ENTRIES", 5)

        for i in range(10):
            (tmp_path / f"file_{i:02d}.txt").write_text("x")

        result = await directory_tree({"path": "."})
        assert "truncated" in result


# ---------------------------------------------------------------------------
# search_files
# ---------------------------------------------------------------------------


class TestSearchFiles:
    async def test_glob_matching(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "a.py").write_text("python")
        (tmp_path / "b.txt").write_text("text")

        result = await search_files({"path": ".", "pattern": "*.py"})
        assert "a.py" in result
        assert "b.txt" not in result

    async def test_max_results(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        for i in range(10):
            (tmp_path / f"file_{i}.txt").write_text("x")

        result = await search_files({"path": ".", "pattern": "*.txt", "max_results": 3})
        assert "3 result" in result

    async def test_no_matches(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        result = await search_files({"path": ".", "pattern": "*.xyz"})
        assert "No files matching" in result


# ---------------------------------------------------------------------------
# search_in_files
# ---------------------------------------------------------------------------


class TestSearchInFiles:
    async def test_text_match(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "code.py").write_text("def hello():\n    return 'world'\n")

        result = await search_in_files({"path": ".", "query": "hello"})
        assert "code.py:1:" in result
        assert "hello" in result

    async def test_line_numbers(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "data.txt").write_text("aaa\nbbb\nccc\nbbb\n")

        result = await search_in_files({"path": ".", "query": "bbb"})
        assert "data.txt:2:" in result
        assert "data.txt:4:" in result

    async def test_binary_files_skipped(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02\x03hello")
        (tmp_path / "text.txt").write_text("hello world")

        result = await search_in_files({"path": ".", "query": "hello"})
        assert "text.txt" in result
        assert "binary.bin" not in result

    async def test_max_results_clamped(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        # Create file with many matching lines
        (tmp_path / "many.txt").write_text("match\n" * 100)

        result = await search_in_files({"path": ".", "query": "match", "max_results": 5})
        assert "5 match" in result


# ---------------------------------------------------------------------------
# file_info
# ---------------------------------------------------------------------------


class TestFileInfo:
    async def test_file_metadata(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "test.txt").write_text("hello world")

        result = await file_info({"path": "test.txt"})
        assert "Type: file" in result
        assert "Size:" in result
        assert "Permissions:" in result
        assert "Modified:" in result

    async def test_dir_metadata(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "subdir").mkdir()

        result = await file_info({"path": "subdir"})
        assert "Type: directory" in result

    async def test_nonexistent(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        result = await file_info({"path": "nope.txt"})
        assert "does not exist" in result


# ---------------------------------------------------------------------------
# file_move
# ---------------------------------------------------------------------------


class TestFileMove:
    async def test_move_file(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "old.txt").write_text("content")

        result = await file_move({"source": "old.txt", "destination": "new.txt"})
        assert "Moved" in result
        assert not (tmp_path / "old.txt").exists()
        assert (tmp_path / "new.txt").read_text() == "content"

    async def test_move_to_subdir(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "file.txt").write_text("content")

        result = await file_move({"source": "file.txt", "destination": "sub/file.txt"})
        assert "Moved" in result
        assert (tmp_path / "sub" / "file.txt").exists()

    async def test_cross_sandbox_blocked(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "file.txt").write_text("content")

        with pytest.raises(SafetyViolationError):
            await file_move({"source": "file.txt", "destination": "../../etc/evil.txt"})

    async def test_destination_exists_error(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")

        result = await file_move({"source": "a.txt", "destination": "b.txt"})
        assert "already exists" in result


# ---------------------------------------------------------------------------
# file_delete
# ---------------------------------------------------------------------------


class TestFileDelete:
    async def test_delete_file(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "doomed.txt").write_text("bye")

        result = await file_delete({"path": "doomed.txt"})
        assert "Deleted file" in result
        assert not (tmp_path / "doomed.txt").exists()

    async def test_non_recursive_dir_error(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        d = tmp_path / "notempty"
        d.mkdir()
        (d / "file.txt").write_text("x")

        result = await file_delete({"path": "notempty"})
        assert "not empty" in result.lower()

    async def test_recursive_dir(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        d = tmp_path / "deleteme"
        d.mkdir()
        (d / "file.txt").write_text("x")

        result = await file_delete({"path": "deleteme", "recursive": True})
        assert "Deleted directory" in result
        assert not d.exists()

    async def test_delete_empty_dir(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "emptydir").mkdir()

        result = await file_delete({"path": "emptydir"})
        assert "Deleted directory" in result

    async def test_protected_sandbox_root(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        with pytest.raises(SafetyViolationError, match="PROTECTED_PATH"):
            await file_delete({"path": "."})
