import asyncio
from browser_tool import BrowserTool

async def test():
    tool = BrowserTool(headless=True)
    await tool.start()
    await tool.goto('https://example.com')
    title = await tool.evaluate('() => document.title')
    print('Title:', title)
    await tool.close()

asyncio.run(test())