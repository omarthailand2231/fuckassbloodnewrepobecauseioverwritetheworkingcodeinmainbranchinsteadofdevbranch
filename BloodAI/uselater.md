# Saved Code — Use Later

## History Scraping on Startup

Previously ran on boot to scrape all channels and populate memory. Removed to simplify startup.

```python
# In on_ready():
#     bot.loop.create_task(sync_history_activity())

async def sync_history_activity():
    log.info("Starting history scraping sync...")
    SKIP_CHANNELS = set(str(x) for x in CONFIG["scrape_skip_channels"])
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if str(channel.id) in SKIP_CHANNELS:
                continue
            if not channel.permissions_for(guild.me).read_message_history:
                continue
            path = memory._channel_md_path(str(guild.id), str(channel.id))
            if os.path.exists(path) and os.path.getsize(path) > CONFIG["scrape_channel_size_cap"]:
                continue
            log.debug("Scraping #%s...", channel.name)
            try:
                messages = []
                async for msg in channel.history(limit=CONFIG["scrape_history_limit"]):
                    if not msg.author.bot:
                        messages.append(msg)
                for msg in reversed(messages):
                    memory.store_message(
                        guild_id=str(guild.id),
                        user_id=str(msg.author.id),
                        username=msg.author.display_name,
                        content=msg.content,
                        channel=channel.name,
                        channel_id=str(channel.id),
                    )
                await asyncio.sleep(CONFIG["scrape_delay"])
            except Exception as e:
                log.warning("Failed to scrape #%s: %s", channel.name, e)
                await asyncio.sleep(CONFIG["scrape_error_delay"])
    log.info("History scraping done.")
```

### Config keys used:
```python
"scrape_skip_channels":     [],
"scrape_history_limit":     1000,
"scrape_channel_size_cap":  60000,
"scrape_delay":             1.5,
"scrape_error_delay":       5.0,
```
