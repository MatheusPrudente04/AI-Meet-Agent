"""Grava a especificação OpenAPI em docs/api/openapi.json.

    docker compose run --rm --no-deps -v "$PWD/docs:/app/docs" api python -m api.export_openapi

(no container, para usar as versões travadas das bibliotecas)

É o contrato para quem constrói um front: importe no Swagger Editor, no Postman
ou gere o cliente com openapi-generator. O teste test_openapi_em_dia falha se as
rotas ou os modelos do arquivo ficarem diferentes do código.
"""
import json
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.json"


def render() -> str:
    from api.main import app
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(render(), encoding="utf-8")
    print(PATH)
