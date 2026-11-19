"""Offline tests for the Studio V2 project catalog."""
import pytest

from hongguo.project_catalog import ProjectCatalog


def test_catalog_save_open_rename_and_clone(tmp_path):
    catalog = ProjectCatalog(tmp_path)
    project = catalog.service.new_project("第一集")
    assert catalog.open(project.id).title == "第一集"
    assert any(row["id"] == project.id for row in catalog.list_projects())
    catalog.rename(project.id, "新的名称")
    assert catalog.open(project.id).title == "新的名称"
    cloned = catalog.duplicate(project.id)
    assert cloned.id != project.id
    assert cloned.title == "新的名称（副本）"
    assert catalog.open(project.id).title == "新的名称"


def test_delete_requires_confirmation(tmp_path):
    catalog = ProjectCatalog(tmp_path)
    project = catalog.service.new_project("测试")
    with pytest.raises(ValueError):
        catalog.delete(project.id)
    assert catalog.open(project.id).id == project.id
    catalog.delete(project.id, confirm=True)
    assert catalog.list_projects() == []


def test_rename_rejects_blank_title(tmp_path):
    catalog = ProjectCatalog(tmp_path)
    project = catalog.service.new_project("测试")
    with pytest.raises(ValueError):
        catalog.rename(project.id, "  ")
