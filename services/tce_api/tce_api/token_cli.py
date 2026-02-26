from __future__ import annotations

import typer

from .token_store import issue_token, list_tokens, revoke_token

app = typer.Typer(help="Manage TCE bearer tokens in OS keyring")


@app.command("issue")
def issue(name: str) -> None:
    token = issue_token(name)
    typer.echo(f"name={name}")
    typer.echo(f"token={token}")


@app.command("list")
def list_cmd() -> None:
    for name in list_tokens():
        typer.echo(name)


@app.command("revoke")
def revoke(name: str) -> None:
    revoke_token(name)
    typer.echo(f"revoked={name}")


if __name__ == "__main__":
    app()
