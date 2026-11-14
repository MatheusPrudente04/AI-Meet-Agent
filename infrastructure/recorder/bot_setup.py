"""
bot_setup.py — Login manual da conta Google do bot.

Abre o MESMO navegador que o bot usa (Chromium do Playwright) no perfil
persistente e espera você concluir o login na janela. Usar outro navegador
(ex.: Google Chrome instalado) no mesmo perfil invalida a sessão, porque as
versões diferem.

Uso:
  python -m infrastructure.recorder.bot_setup
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
            args=["--disable-blink-features=AutomationControlled"],
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


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
