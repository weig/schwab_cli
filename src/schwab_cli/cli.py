import typer

app = typer.Typer(
    name="schwab_cli",
    help="Charles Schwab CLI.",
    no_args_is_help=True,
    add_completion=False,
    invoke_without_command=True,
)


@app.callback()
def main() -> None:
    """Charles Schwab CLI."""
