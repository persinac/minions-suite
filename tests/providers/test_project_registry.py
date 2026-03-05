"""Tests for project registry and profile inference."""


import pytest
import yaml

from minions.project_registry import (
    ProjectRegistryError,
    build_registry,
    infer_profile,
)

# -----------------------------------------------------------------------
# Profile inference
# -----------------------------------------------------------------------


class TestInferProfile:
    def test_python_files(self):
        profile = infer_profile(["src/main.py", "src/utils.py"])
        assert "python" in profile.languages

    def test_typescript_files(self):
        profile = infer_profile(["src/App.tsx", "src/index.ts"])
        assert "typescript" in profile.languages

    def test_javascript_maps_to_typescript(self):
        profile = infer_profile(["index.js"])
        assert "typescript" in profile.languages

    def test_go_files(self):
        profile = infer_profile(["main.go"])
        assert "go" in profile.languages

    def test_sql_files(self):
        profile = infer_profile(["db/migration.sql"])
        assert "sql" in profile.languages
        assert "data_engineer" in profile.roles

    def test_shell_files(self):
        profile = infer_profile(["scripts/deploy.sh"])
        assert "shell" in profile.languages

    def test_dockerfile_detects_devops(self):
        profile = infer_profile(["Dockerfile"])
        assert "devops" in profile.roles

    def test_github_actions_detects_devops(self):
        profile = infer_profile([".github/workflows/ci.yml"])
        assert "devops" in profile.roles

    def test_api_paths_detect_backend(self):
        profile = infer_profile(["src/api/handler.py"])
        assert "backend" in profile.roles

    def test_components_detect_frontend(self):
        profile = infer_profile(["src/components/Button.tsx"])
        assert "frontend" in profile.roles

    def test_migration_detects_data_engineer(self):
        profile = infer_profile(["alembic/versions/001.py"])
        assert "data_engineer" in profile.roles

    def test_mixed_files(self):
        profile = infer_profile([
            "src/api/handler.py",
            "src/components/Button.tsx",
            "db/migration.sql",
        ])
        assert "python" in profile.languages
        assert "typescript" in profile.languages
        assert "sql" in profile.languages
        assert "backend" in profile.roles
        assert "frontend" in profile.roles

    def test_empty_files(self):
        profile = infer_profile([])
        assert profile.roles == []
        assert profile.languages == []

    def test_results_sorted(self):
        profile = infer_profile(["z.py", "a.ts", "b.go"])
        assert profile.languages == sorted(profile.languages)


# -----------------------------------------------------------------------
# Registry loading
# -----------------------------------------------------------------------


class TestBuildRegistry:
    def test_load_simple_config(self, tmp_path):
        config = {
            "defaults": {"model": "gpt-4o", "git_provider": "gitlab"},
            "projects": {
                "my-project": {
                    "project_id": "team/my-project",
                    "review_profile": {"roles": ["backend"], "languages": ["python"]},
                }
            },
        }
        config_path = tmp_path / "projects.yaml"
        config_path.write_text(yaml.dump(config))
        registry = build_registry(str(config_path))
        assert "my-project" in registry
        proj = registry["my-project"]
        assert proj.project_id == "team/my-project"
        assert proj.model == "gpt-4o"
        assert "backend" in proj.review_profile.roles

    def test_missing_config_raises(self):
        with pytest.raises(ProjectRegistryError):
            build_registry("/nonexistent/projects.yaml")

    def test_empty_config(self, tmp_path):
        config_path = tmp_path / "projects.yaml"
        config_path.write_text("{}")
        registry = build_registry(str(config_path))
        assert registry == {}

    def test_project_with_services(self, tmp_path):
        config = {
            "projects": {
                "multi-svc": {
                    "project_id": "team/multi",
                    "services": {
                        "api": {
                            "clone_url": "git@example.com:api.git",
                            "language": "python",
                            "test_command": "pytest",
                        },
                        "web": {
                            "clone_url": "git@example.com:web.git",
                            "language": "typescript",
                        },
                    },
                }
            },
        }
        config_path = tmp_path / "projects.yaml"
        config_path.write_text(yaml.dump(config))
        registry = build_registry(str(config_path))
        proj = registry["multi-svc"]
        assert len(proj.services) == 2
        assert proj.services["api"].language == "python"
        assert proj.services["web"].language == "typescript"

    def test_project_with_issues_config(self, tmp_path):
        config = {
            "projects": {
                "proj": {
                    "project_id": "team/proj",
                    "issues": {"enabled": True, "label": "auto"},
                }
            },
        }
        config_path = tmp_path / "projects.yaml"
        config_path.write_text(yaml.dump(config))
        registry = build_registry(str(config_path))
        assert registry["proj"].issues.enabled is True
        assert registry["proj"].issues.label == "auto"

    def test_defaults_applied(self, tmp_path):
        config = {
            "defaults": {"model": "claude-3", "gitlab_url": "https://gl.example.com"},
            "projects": {
                "proj": {"project_id": "team/proj"},
            },
        }
        config_path = tmp_path / "projects.yaml"
        config_path.write_text(yaml.dump(config))
        registry = build_registry(str(config_path))
        assert registry["proj"].model == "claude-3"
        assert registry["proj"].gitlab_url == "https://gl.example.com"
