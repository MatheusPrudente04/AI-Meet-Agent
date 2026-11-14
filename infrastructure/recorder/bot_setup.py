"""
bot_setup.py — Autenticacao automatica do bot (rodar UMA unica vez).

Usa Playwright para fazer login no Google e salvar o perfil com a sessao
autenticada. O perfil salvo em bot_chrome_profile e reutilizado pelo
Playwright nas execucoes seguintes.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


async def main():
    from dotenv import load_dotenv
    load_dotenv()

    email = os.getenv("BOT_GOOGLE_EMAIL", "")
    password = os.getenv("BOT_GOOGLE_PASSWORD", "")
    profile_dir = os.getenv("BOT_CHROME_PROFILE", "./bot_chrome_profile")

    if not email or not password:
        print("\nConfigure no .env:")
        print("   BOT_GOOGLE_EMAIL=seubot@gmail.com")
        print("   BOT_GOOGLE_PASSWORD=sua_senha")
        sys.exit(1)

    from playwright.async_api import async_playwright

    print("\nMeet Agent Bot - Setup de autenticacao")
    print(f"   Email: {email}")
    print(f"   Perfil: {profile_dir}")
    print("=" * 50)

    os.makedirs(profile_dir, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=os.path.abspath(profile_dir),
            headless=False,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        try:
            print("\nNavegando para login do Google...")
            await page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded")
            await asyncio.sleep(3)

            # Se houver seletor de conta, clica "Usar outra conta"
            try:
                outra_conta = page.locator(
                    '[jsname="dRO4D"], text="Usar outra conta", text="Use another account"'
                )
                if await outra_conta.first.is_visible(timeout=3000):
                    await outra_conta.first.click()
                    await asyncio.sleep(1)
            except Exception:
                pass

            print("Preenchendo email...")
            email_field = page.locator('input[type="email"], #identifierId')
            await email_field.first.wait_for(timeout=15000)
            await email_field.first.fill(email)
            await asyncio.sleep(1)
            await page.keyboard.press("Enter")
            await asyncio.sleep(3)

            print("Preenchendo senha...")
            pw_field = page.locator('input[type="password"]')
            await pw_field.first.wait_for(timeout=15000)
            await pw_field.first.fill(password)
            await asyncio.sleep(1)
            await page.keyboard.press("Enter")

            print("\nAguardando conclusao do login...")
            for _ in range(180):
                current_url = page.url
                if "myaccount.google.com" in current_url or (
                    "google.com" in current_url and "signin" not in current_url
                ):
                    break
                await asyncio.sleep(1)
            else:
                raise TimeoutError("Tempo limite excedido aguardando login")

            print("Login realizado com sucesso!")
            print(f"Perfil salvo em: {profile_dir}")

        except TimeoutError:
            print("\nTempo limite excedido.")
            print("   Complete o login manualmente e pressione Enter...")
            try:
                input()
            except EOFError:
                pass
            print("Continuando...")

        except Exception as e:
            print(f"\nAtencao - Erro na automacao: {e}")
            print("   O navegador pode ter solicitado verificacao em 2 etapas.")
            print("   Complete manualmente e pressione Enter...")
            try:
                input()
            except EOFError:
                pass
            print("Continuando...")

        finally:
            await context.close()
            print("Browser fechado. Perfil pronto para uso.")


if __name__ == "__main__":
    asyncio.run(main())
