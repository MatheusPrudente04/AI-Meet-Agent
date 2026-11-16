"""
bot_setup.py — Login manual da conta Google do bot.

Abre o MESMO navegador que o bot usa (Chromium do Playwright) no perfil
persistente e espera você concluir o login na janela. Usar outro navegador
(ex.: Google Chrome instalado) no mesmo perfil invalida a sessão, porque as
versões diferem.

Uso:
  python -m infrastructure.recorder.bot_setup
  docker compose --profile bot run --rm --service-ports bot python -m infrastructure.recorder.bot_setup
      (bot em container: abra http://localhost:6080/vnc.html e faça o login na tela que aparece)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

LOGIN_TIMEOUT_S = 300


async def _logged_in(context) -> bool:
    cookies = await context.cookies("https://accounts.google.com")
    return any(c["name"] == "SID" for c in cookies)


async def main() -> int:
    from config.settings import get_settings
    from playwright.async_api import async_playwright

    profile_dir = os.path.abspath(get_settings().bot_chrome_profile)
    os.makedirs(profile_dir, exist_ok=True)
    print(f"Perfil: {profile_dir}")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            profile_dir,
            headless=False,
            # No container Linux (S10-1) o Docker não dá ao Chromium o namespace do sandbox;
            # o bot já roda com --no-sandbox, e o login usa o mesmo.
            args=["--disable-blink-features=AutomationControlled"]
                 + (["--no-sandbox"] if sys.platform.startswith("linux") else []),
            ignore_default_args=["--enable-automation"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://accounts.google.com/", wait_until="domcontentloaded")

        if await _logged_in(context):
            print("Perfil já está logado.")
            await context.close()
            return 0

        print(f"Faça o login da conta do bot na janela aberta (até {LOGIN_TIMEOUT_S // 60} min)...")
        for _ in range(LOGIN_TIMEOUT_S):
            await asyncio.sleep(1)
            if await _logged_in(context):
                await asyncio.sleep(3)  # deixa o Chromium gravar os cookies em disco
                print("Login concluído. Perfil pronto para uso.")
                await context.close()
                return 0

        print("ERRO: tempo esgotado sem login.", file=sys.stderr)
        await context.close()
        return 1


# ── Credenciamento pela interface (task setup_bot, fila do bot) ───────────────────────

WAIT_HUMAN_S = 600   # tempo para alguém concluir o login pela tela do bot
AUTO_LOGIN_S = 45    # tempo para o login automático virar cookie de sessão


async def credential(report, email: str = "", password: str = "", check_only: bool = False,
                     wait_human_s: int = WAIT_HUMAN_S) -> str:
    """Deixa o perfil do bot logado na conta do Google e devolve o status final.

    report(status, mensagem) grava o andamento (BotAccountStore.set). Ordem:
    1. perfil já logado → ready;
    2. com e-mail e senha: digita os dois e espera a sessão;
    3. sem eles, ou se o Google pedir verificação (código, celular, captcha): waiting_login,
       e alguém conclui na tela do bot (noVNC). Nada aqui tenta contornar a verificação.
    check_only: só confere (ready ou not_configured), sem abrir janela nem esperar ninguém.
    """
    from config.settings import get_settings
    from playwright.async_api import async_playwright

    profile_dir = os.path.abspath(get_settings().bot_chrome_profile)
    os.makedirs(profile_dir, exist_ok=True)
    if not check_only:
        report("creating", "Abrindo o navegador do bot")
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            profile_dir,
            headless=check_only,
            args=["--disable-blink-features=AutomationControlled"]
                 + (["--no-sandbox"] if sys.platform.startswith("linux") else []),
            ignore_default_args=["--enable-automation"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://accounts.google.com/", wait_until="domcontentloaded")
            if await _logged_in(context):
                report("ready", "Conta do bot conectada")
                return "ready"
            if check_only:
                report("not_configured", "O bot ainda não está conectado a uma conta do Google")
                return "not_configured"

            if email and password:
                report("creating", "Entrando na conta do bot")
                if await _auto_login(page, context, email, password):
                    report("ready", "Conta do bot conectada")
                    return "ready"

            report("waiting_login", "Conclua o login da conta do bot na tela abaixo"
                   + (" (o Google pediu uma verificação)" if email and password else ""))
            for _ in range(wait_human_s):
                await asyncio.sleep(1)
                if await _logged_in(context):
                    await asyncio.sleep(3)  # deixa o Chromium gravar os cookies em disco
                    report("ready", "Conta do bot conectada")
                    return "ready"
            report("error", f"Ninguém concluiu o login em {wait_human_s // 60} min. Tente de novo.")
            return "error"
        except Exception as exc:
            report("error", f"Falha ao credenciar o bot: {str(exc).splitlines()[0][:200]}")
            return "error"
        finally:
            await context.close()


async def _auto_login(page, context, email: str, password: str) -> bool:
    """Digita e-mail e senha nas telas padrão do Google. False se aparecer qualquer outra
    tela (verificação em duas etapas, captcha, "navegador não seguro"): aí entra o humano."""
    try:
        await page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded")
        await page.fill('input[type="email"]', email, timeout=15000)
        await page.click("#identifierNext")
        await page.fill('input[type="password"]', password, timeout=15000)
        await page.click("#passwordNext")
    except Exception:
        return False
    for _ in range(AUTO_LOGIN_S):
        await asyncio.sleep(1)
        if await _logged_in(context):
            await asyncio.sleep(3)
            return True
    return False


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
