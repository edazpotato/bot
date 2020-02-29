# -*- coding: utf-8 -*-

"""
jishaku.cog
~~~~~~~~~~~

The Jishaku debugging and diagnostics cog.

:copyright: (c) 2019 Devon (Gorialis) R
:license: MIT, see LICENSE for more details.

"""

import asyncio
import aiohttp
import collections
import contextlib
import datetime
import inspect
import itertools
import io
import os
import os.path
import re
import sys
import time
import json
import traceback
import typing

import discord
import humanize
from discord.ext import commands
from fire.push import pushbullet
from fire.converters import User, UserWithFallback, Member, TextChannel, VoiceChannel

from jishaku.codeblocks import Codeblock, CodeblockConverter
from jishaku.exception_handling import ReplResponseReactor
from jishaku.meta import __version__
from jishaku.models import copy_context_with
from jishaku.modules import ExtensionConverter, package_version
from jishaku.paginators import PaginatorInterface, WrappedFilePaginator, WrappedPaginator, PaginatorEmbedInterface
from jishaku.repl import AsyncCodeExecutor, Scope, all_inspections, get_var_dict_from_ctx
from jishaku.shell import ShellReader
from jishaku.voice import BasicYouTubeDLSource, connected_check, playing_check, vc_check, youtube_dl

try:
    import psutil
except ImportError:
    psutil = None

__all__ = (
    "Jishaku",
    "setup"
)

ENABLED_SYMBOLS = ("true", "t", "yes", "y", "on", "1")
JISHAKU_HIDE = True
JISHAKU_RETAIN = os.getenv("JISHAKU_RETAIN", "").lower() in ENABLED_SYMBOLS


CommandTask = collections.namedtuple("CommandTask", "index ctx task")

print('Loading Jishaku....')


class Jishaku(commands.Cog):  # pylint: disable=too-many-public-methods
    """
    The cog that includes Jishaku's Discord-facing default functionality.
    """

    load_time = datetime.datetime.now()

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._scope = Scope()
        self.retain = JISHAKU_RETAIN
        self.last_evalmsg = 0
        self.last_eval = 0
        self.last_eval_paginator = 0
        self.last_result = None
        self.start_time = datetime.datetime.now()
        self.tasks = collections.deque()
        self.task_count: int = 0
        self.bot.acknowledgements = {}

    @property
    def scope(self):
        """
        Gets a scope for use in REPL.

        If retention is on, this is the internal stored scope,
        otherwise it is always a new Scope.
        """

        if self.retain:
            return self._scope
        return Scope()

    @contextlib.contextmanager
    def submit(self, ctx: commands.Context):
        """
        A context-manager that submits the current task to jishaku's task list
        and removes it afterwards.

        Arguments
        ---------
        ctx: commands.Context
            A Context object used to derive information about this command task.
        """

        self.task_count += 1
        cmdtask = CommandTask(self.task_count, ctx, asyncio.Task.current_task())
        self.tasks.append(cmdtask)

        try:
            yield cmdtask
        finally:
            if cmdtask in self.tasks:
                self.tasks.remove(cmdtask)

    async def cog_check(self, ctx: commands.Context):
        """
        Local check, makes all commands in this cog owner-only
        """
        if ctx.bot.authenticated:
            if not await ctx.bot.is_team_owner(ctx.author):
                raise commands.NotOwner("You must own this bot to use Jishaku.")
            return True
        else:
            noauth = await ctx.send('<:xmark:674359427830382603> Not Authenticated! Authenticate now to continue.')
            try:
                await self.bot.wait_for('admin_authenticate', timeout=30.0)
                await noauth.edit(content='<:check:674359197378281472> Successfully authenticated! Executing command...')
                if await ctx.bot.is_team_owner(ctx.author):
                    return True
                else:
                    raise commands.NotOwner("You must own this bot to use Jishaku.")
            except asyncio.TimeoutError:
                await noauth.edit(content='<:xmark:674359427830382603> Not Authenticated!')

    async def loadacks(self):
        self.bot.acknowledgements = {}
        query = 'SELECT * FROM ack;'
        acks = await self.bot.db.fetch(query)
        for a in acks:
            self.bot.acknowledgements[a['uid']] = a['acks']

    @commands.Cog.listener()
    async def on_ready(self):
        await self.loadacks()

    @commands.group(name="admin", aliases=["administration", "jsk"], hidden=JISHAKU_HIDE,
                    invoke_without_command=True, ignore_extra=False)
    async def jsk(self, ctx: commands.Context):
        """
        The Jishaku debug and diagnostic commands.

        This command on its own gives a status brief.
        All other functionality is within its subcommands.
        """

        summary = [
            f"Jishaku v{__version__}, discord.py `{package_version('discord.py')}`, "
            f"`Python {sys.version}` on `{sys.platform}`".replace("\n", ""),
            f"Module was loaded {humanize.naturaltime(self.load_time)}, "
            f"cog was loaded {humanize.naturaltime(self.start_time)}.",
            ""
        ]

        if psutil:
            proc = psutil.Process()

            with proc.oneshot():
                mem = proc.memory_full_info()
                summary.append(f"Using {humanize.naturalsize(mem.rss)} physical memory and "
                               f"{humanize.naturalsize(mem.vms)} virtual memory, "
                               f"{humanize.naturalsize(mem.uss)} of which unique to this process.")

                name = proc.name()
                pid = proc.pid
                thread_count = proc.num_threads()

                summary.append(f"Running on PID {pid} (`{name}`) with {thread_count} thread(s).")

                summary.append("")  # blank line

        cache_summary = f"{len(self.bot.guilds)} guild(s) and {len(self.bot.users)} user(s)"

        # if isinstance(self.bot, discord.AutoShardedClient):
        #     summary.append(f"This bot is automatically sharded and can see {cache_summary}.")
        # elif self.bot.shard_count:
        #     summary.append(f"This bot is manually sharded and can see {cache_summary}.")
        # else:
        summary.append(f"This bot is not sharded and can see {cache_summary}.")

        summary.append(f"Average websocket latency: {round(self.bot.latency * 1000, 2)}ms")

        await ctx.send("\n".join(summary))

    __cat_line_regex = re.compile(r"(?:\.\/+)?(.+?)(?:#L?(\d+)(?:\-L?(\d+))?)?$")

    @jsk.command(name="cat")
    async def jsk_cat(self, ctx: commands.Context, argument: str):
        """
        Read out a file, using syntax highlighting if detected.

        Lines and linespans are supported by adding '#L12' or '#L12-14' etc to the end of the filename.
        """

        match = self.__cat_line_regex.search(argument)

        if not match:  # should never happen
            return await ctx.send("Couldn't parse this input.")

        path = match.group(1)

        line_span = None

        if match.group(2):
            start = int(match.group(2))
            line_span = (start, int(match.group(3) or start))

        if not os.path.exists(path) or os.path.isdir(path):
            return await ctx.send(f"`{path}`: No file by that name.")

        size = os.path.getsize(path)

        if size <= 0:
            return await ctx.send(f"`{path}`: Cowardly refusing to read a file with no size stat"
                                  f" (it may be empty, endless or inaccessible).")

        if size > 50 * (1024 ** 2):
            return await ctx.send(f"`{path}`: Cowardly refusing to read a file >50MB.")

        try:
            with open(path, "rb") as file:
                paginator = WrappedFilePaginator(file, line_span=line_span, max_size=1985)
        except UnicodeDecodeError:
            return await ctx.send(f"`{path}`: Couldn't determine the encoding of this file.")
        except ValueError as exc:
            return await ctx.send(f"`{path}`: Couldn't read this file, {exc}")

        interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
        await interface.send_to(ctx)

    @jsk.command(name="curl")
    async def jsk_curl(self, ctx: commands.Context, url: str):
        """
        Download and display a text file from the internet.
        This command is similar to jsk cat, but accepts a URL.
        """

        # remove embed maskers if present
        url = url.lstrip("<").rstrip(">")
        if 'api.hypixel.net' in url:
            if '?' in url:
                url = f'{url}&key={self.bot.hypixelkey}'
            else:
                url = f'{url}?key={self.bot.hypixelkey}'

        async with ReplResponseReactor(ctx.message):
            async with aiohttp.ClientSession(headers={'User-Agent': 'Fire Discord Bot'}) as session:
                async with session.get(url) as response:
                    if 'application/json' in response.headers.get('Content-Type', 'text/html').lower():
                        data = await response.json()
                        data = json.dumps(data, indent=2).encode('utf-8')
                    else:
                        data = await response.read()
                    hints = (
                        response.content_type,
                        url
                    )
                    code = response.status

            if not data:
                return await ctx.send(f"HTTP response was empty (status code {code}).")

            try:
                paginator = WrappedFilePaginator(io.BytesIO(data), language_hints=hints, max_size=1985)
            except UnicodeDecodeError:
                return await ctx.send(f"Couldn't determine the encoding of the response. (status code {code})")
            except ValueError as exc:
                return await ctx.send(f"Couldn't read response (status code {code}), {exc}")

            interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
            await interface.send_to(ctx)

    @jsk.command(name="tasks")
    async def jsk_tasks(self, ctx: commands.Context):
        """
        Shows the currently running jishaku tasks.
        """

        if not self.tasks:
            return await ctx.send("No currently running tasks.")

        paginator = commands.Paginator(max_size=1985)

        for task in self.tasks:
            paginator.add_line(f"{task.index}: `{task.ctx.command.qualified_name}`, invoked at "
                               f"{task.ctx.message.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")

        interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
        await interface.send_to(ctx)

    @jsk.command(name="cancel")
    async def jsk_cancel(self, ctx: commands.Context, index: int, *, name: str = None):
        """
        Cancels a task with the given index.

        If the index passed is -1, will cancel the last task instead.
        """

        if not self.tasks:
            return await ctx.send("No tasks to cancel.")

        if index == -1 and not name:
            task = self.tasks.pop()
        if index == -1 and name:
            for task in self.tasks.copy():
                if task.ctx.command.qualified_name == name:
                    self.tasks.remove(task)
            return await ctx.success('Successfully canceled tasks')
        else:
            task = discord.utils.get(self.tasks, index=index)
            if task:
                self.tasks.remove(task)
            else:
                return await ctx.send("Unknown task.")

        task.task.cancel()
        return await ctx.send(f"Cancelled task {task.index}: `{task.ctx.command.qualified_name}`,"
                              f" invoked at {task.ctx.message.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    @jsk.command(name="retain")
    async def jsk_retain(self, ctx: commands.Context, *, toggle: bool):
        """
        Turn variable retention for REPL on or off.
        """

        if toggle:
            if self.retain:
                return await ctx.send("Variable retention is already set to ON.")

            self.retain = True
            self._scope = Scope()
            return await ctx.send("Variable retention is ON. Future REPL sessions will retain their scope.")

        if not self.retain:
            return await ctx.send("Variable retention is already set to OFF.")

        self.retain = False
        return await ctx.send("Variable retention is OFF. Future REPL sessions will dispose their scope when done.")

    @jsk.command(name="py", aliases=["python"])
    async def jsk_python(self, ctx: commands.Context, *, argument: CodeblockConverter):
        """
        Direct evaluation of Python code.
        """

        arg_dict = get_var_dict_from_ctx(ctx)

        scope = self.scope

        scope.clean()
        arg_dict["_"] = self.last_result

        async with ReplResponseReactor(ctx.message, argument=argument):
            with self.submit(ctx):
                start = time.perf_counter()
                async for result in AsyncCodeExecutor(argument.content, scope, arg_dict=arg_dict):
                    end = time.perf_counter()
                    if result is None:
                        continue

                    resultstr = str(result)
                    self.last_result = result
                    resulttype = result.__class__.__name__
                    if self.bot.http.token in resultstr:
                        result = '[result hidden for security reasons]'
                        await pushbullet('note', 'Attempted Token Leak', f'{ctx.author} attempted to retrieve Fire\'s token', 'https://api.gaminggeek.club')
                        return
                    tokenlist = self.bot.http.token.split('.')
                    if any(x in resultstr for x in tokenlist):
                        result = '[result hidden for security reasons]'
                        await pushbullet('note', 'Attempted Token Leak', f'{ctx.author} attempted to retrieve Fire\'s token', 'https://api.gaminggeek.club')
                        return

                    try:
                        last_eval = await ctx.channel.fetch_message(self.last_eval)
                    except Exception:
                        last_eval = 0

                    try:
                        last_eval_paginator = await ctx.channel.fetch_message(self.last_eval_paginator)
                    except Exception:
                        last_eval_paginator = 0

                    if not ctx.message.edited_at or ctx.message.id != self.last_evalmsg:
                        last_eval = 0
                        last_eval_paginator = 0
                        self.last_evalmsg = ctx.message.id

                    if isinstance(result, discord.File):
                        await ctx.send(file=result)
                    elif isinstance(result, discord.Embed):
                        if type(last_eval) == discord.Message:
                            await last_eval.edit(embed=result)
                        else:
                            last_eval = await ctx.send(embed=result)
                            self.last_eval = last_eval.id
                    elif isinstance(result, PaginatorInterface):
                        if type(last_eval) == discord.Message:
                            await result.send_edit(last_eval)
                        else:
                            last_eval = await result.send_to(ctx)
                            self.last_eval = last_eval.id
                    elif isinstance(result, discord.Message) and result.id > ctx.message.id:
                        return
                    else:
                        if not isinstance(result, str):
                            # repr all non-strings
                            result = repr(result)

                        result = result.replace("``", "`\u200b`")

                        if len(result) > 1024:
                            # inconsistency here, results get wrapped in codeblocks when they are too large
                            #  but don't if they're not. probably not that bad, but noting for later review
                            paginator = WrappedPaginator(prefix='```py', suffix='```', max_size=1985)
                            paginator.add_line(result)
                            embed = discord.Embed(title="<:check:674359197378281472> Evaluation Complete", colour=ctx.author.color, description=f"Output Type: {resulttype}")
                            embed.add_field(name=":inbox_tray: Input", value=f"```py\n{argument.content}```", inline=False)
                            embed.set_footer(text=f'Evaluated in {end - start:.3f}s.')
                            paginatorembed = discord.Embed(colour=ctx.author.color)
                            interface = PaginatorEmbedInterface(ctx.bot, paginator, owner=ctx.author, _embed=paginatorembed)
                            if type(last_eval) == discord.Message:
                                await last_eval.edit(embed=embed)
                            else:
                                last_eval = await ctx.send(embed=embed)
                                self.last_eval = last_eval.id
                            if type(last_eval_paginator) == discord.Message:
                                await interface.send_edit(last_eval_paginator)
                            else:
                                last_eval_paginator = await interface.send_to(ctx)
                                self.last_eval_paginator = last_eval_paginator.message.id
                        else:
                            if result.strip() == '':
                                result = "\u200b"
                            embed = discord.Embed(title="<:check:674359197378281472> Evaluation Complete", colour=ctx.author.color, description=f"Output Type: {resulttype}")
                            embed.add_field(name=":inbox_tray: Input", value=f"```py\n{argument.content}```", inline=False)
                            embed.add_field(name=":outbox_tray: Output", value=f"```py\n{result}```", inline=False)
                            embed.set_footer(text=f'Evaluated in {end - start:.3f}s.')
                            if type(last_eval) == discord.Message:
                                await last_eval.edit(embed=embed)
                            else:
                                last_eval = await ctx.send(embed=embed)
                                self.last_eval = last_eval.id

    @jsk.command(name="py_inspect", aliases=["pyi", "python_inspect", "pythoninspect"])
    async def jsk_python_inspect(self, ctx: commands.Context, *, argument: CodeblockConverter):
        """
        Evaluation of Python code with inspect information.
        """
        if ctx.author.id == 376817315830038530:
            return await ctx.send('No eval for you buddy!!! This is my bot, this is MY land, you CANNOT come close to the might of Geek')

        arg_dict = get_var_dict_from_ctx(ctx)

        scope = self.scope

        scope.clean()
        arg_dict["_"] = self.last_result

        async with ReplResponseReactor(ctx.message, argument=argument):
            with self.submit(ctx):
                async for result in AsyncCodeExecutor(argument.content, scope, arg_dict=arg_dict):
                    resultstr = str(result)
                    self.last_result = result
                    if self.bot.http.token in resultstr:
                        result = '[result hidden for security reasons]'
                        await pushbullet('note', 'Attempted Token Leak', f'{ctx.author} attempted to retrieve Fire\'s token', 'https://api.gaminggeek.club')
                        return
                    tokenlist = self.bot.http.token.split('.')
                    if any(x in resultstr for x in tokenlist):
                        result = '[result hidden for security reasons]'
                        await pushbullet('note', 'Attempted Token Leak', f'{ctx.author} attempted to retrieve Fire\'s token', 'https://api.gaminggeek.club')
                        return

                    header = repr(result).replace("``", "`\u200b`")

                    if len(header) > 485:
                        header = header[0:482] + "..."

                    output = []
                    output.append(f'```prolog\n=== {header} ===\n')
                    for name, res in all_inspections(result):
                        output.append(f"{name:16.16} :: {res}")
                    output.append('```')
                    res = '\n'.join(output)

                    embed = discord.Embed(title="<:check:674359197378281472> Evaluation Complete", colour=ctx.author.color)
                    embed.add_field(name=":inbox_tray: Input", value=f"```py\n{argument.content}```", inline=False)
                    embed.add_field(name=":outbox_tray: Output", value=f"{res}", inline=False)
                    await ctx.send(embed=embed)

    @jsk.command(name='js', aliases=['node', 'nodejs'])
    async def jsk_node(self, ctx: commands.Context, *, argument: CodeblockConverter):
        code = argument.content.replace('"', '\'').replace('\n', '\\n')
# 		js2exc = '''try {{
#   var evalout = eval("{0}");
# }} catch(error) {{
#   var evalerr = error;
# }}

# if(!evalerr) {{
# 	var evalerr = "NO ERROR";
# }}

# const evaloutput = {{
#     "error": evalerr.toString()
# }}

# console.log('EVAL ERROR ' + JSON.stringify(evaloutput));'''.format(code)

        js2exc = '''try {{
    let code = "{0}";
    let result = eval(code);

    if (typeof result !== 'string')
        result = require('util').inspect(result, {{depth: 0}});
    console.log(result);
}} catch (e) {{
    throw new Error(e)
}}'''.format(code)

        with open('main.js', 'w') as m:
            m.write(js2exc)

        output = []

        async with ReplResponseReactor(ctx.message):
            with self.submit(ctx):
                with ShellReader('node main.js') as reader:
                    async for line in reader:
                        output.append(line)

            output = '\n'.join(output).replace('[stderr] ', '')
            print(output)
            if output == 'undefined':
                pass
            else:
                output = output.replace('\nundefined', '')

            if 'throw new Error(e)' in output:
                raise Exception(output.split('Error:')[-1])

            if output and len(output) > 1024:
                # inconsistency here, results get wrapped in codeblocks when they are too large
                #  but don't if they're not. probably not that bad, but noting for later review
                paginator = WrappedPaginator(prefix='```js', suffix='```', max_size=1985)
                paginator.add_line(output)
                embed = discord.Embed(title="<:check:674359197378281472> Evaluation Complete", colour=ctx.author.color)
                embed.add_field(name=":inbox_tray: Input", value=f"```js\n{argument.content}```", inline=False)
                paginatorembed = discord.Embed(colour=ctx.author.color)
                interface = PaginatorEmbedInterface(ctx.bot, paginator, owner=ctx.author, _embed=paginatorembed)
                await ctx.send(embed=embed)
                await interface.send_to(ctx)
            else:
                embed = discord.Embed(title="<:check:674359197378281472> Evaluation Complete", colour=ctx.author.color)
                embed.add_field(name=":inbox_tray: Input", value=f"```js\n{argument.content}```", inline=False)
                embed.add_field(name=":outbox_tray: Output", value=f"```js\n{output}```", inline=False)
                await ctx.send(embed=embed)

        os.remove('main.js')

    @jsk.command(name="shell", aliases=["sh", "cmd"])
    async def jsk_shell(self, ctx: commands.Context, *, argument: CodeblockConverter):
        """
        Executes statements in the system shell.

        This uses the bash shell. Execution can be cancelled by closing the paginator.
        """

        async with ReplResponseReactor(ctx.message):
            with self.submit(ctx):
                paginator = WrappedPaginator(prefix="```sh", max_size=1985)
                paginator.add_line(f"$ {argument.content}\n")

                interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
                self.bot.loop.create_task(interface.send_to(ctx))

                with ShellReader(argument.content) as reader:
                    async for line in reader:
                        if interface.closed:
                            return
                        await interface.add_line(line)

                await interface.add_line(f"\n[status] Return code {reader.close_code}")

    @jsk.command(name="git")
    async def jsk_git(self, ctx: commands.Context, *, argument: CodeblockConverter):
        """
        Shortcut for 'jsk sh git'. Invokes the system shell.
        """

        return await ctx.invoke(self.jsk_shell, argument=Codeblock(argument.language, "git " + argument.content))

    @jsk.command(name="pm2")
    async def jsk_pm2(self, ctx: commands.Context, *, argument: CodeblockConverter):
        """
        Shortcut for 'jsk sh pm2'. Invokes the system shell.
        """

        return await ctx.invoke(self.jsk_shell, argument=Codeblock(argument.language, "pm2 " + argument.content))

    @jsk.command(name="pwrsh")
    async def jsk_powershell(self, ctx: commands.Context, *, argument: CodeblockConverter):
        """
        Shortcut for 'jsk sh powershell'. Invokes the system shell.
        """

        return await ctx.invoke(self.jsk_shell, argument=Codeblock(argument.language, "powershell " + argument.content))

    @jsk.command(name="load", aliases=["reload"])
    async def jsk_load(self, ctx: commands.Context, *extensions: ExtensionConverter):
        """
        Loads or reloads the given extension names.

        Reports any extensions that failed to load.
        """

        paginator = commands.Paginator(prefix='', suffix='')

        for extension in itertools.chain(*extensions):
            if extension == 'api.main':
                try:
                    await self.bot.get_cog('Fire API').stop()
                except Exception:
                    do = 'nothing'
                modules = sys.modules.copy()
                for m in modules:
                    if m.startswith('api.endpoints'):
                        sys.modules.pop(m)
            method, icon = (
                (self.bot.reload_extension, "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}")
                if extension in self.bot.extensions else
                (self.bot.load_extension, "\N{INBOX TRAY}")
            )
            try:
                method(extension)
            except Exception as exc:  # pylint: disable=broad-except
                traceback_data = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__, 1))

                paginator.add_line(
                    f"{icon}\N{WARNING SIGN} `{extension}`\n```py\n{traceback_data}\n```",
                    empty=True
                )
            else:
                paginator.add_line(f"{icon} `{extension}`", empty=True)

        await self.loadacks()
        # await self.bot.get_cog('Settings').loadSettings()
        # await self.bot.get_cog('Utility Commands').loadvanitys()
        # await self.bot.get_cog('Utility Commands').loadfollowable()
        # await self.bot.get_cog('Utility Commands').loadfollows()
        # await self.bot.get_cog('Utility Commands').loadtags()
        # await self.bot.get_cog('Utility Commands').loaddescs()
        # await self.bot.get_cog('Utility Commands').loadremind()
        # await self.bot.get_cog('Mod Commands').loadMutes()
        # await self.bot.get_cog('Mod Commands').loadwarns()
        # await self.bot.get_cog('Mod Commands').loadmodlogs()
        # await self.bot.get_cog('Premium Commands').loadPremiumGuilds()
        # await self.bot.get_cog('Premium Commands').loadAutoroles()
        # await self.bot.get_cog('Premium Commands').loadReactroles()
        # await self.bot.get_cog('Premium Commands').loadJoinRoles()
        # await self.bot.get_cog('Premium Commands').loadRolePersist()

        for page in paginator.pages:
            await ctx.send(page)

    @jsk.command(name="unload")
    async def jsk_unload(self, ctx: commands.Context, *extensions: ExtensionConverter):
        """
        Unloads the given extension names.

        Reports any extensions that failed to unload.
        """

        paginator = commands.Paginator(prefix='', suffix='')
        icon = "\N{OUTBOX TRAY}"

        for extension in itertools.chain(*extensions):
            try:
                if extension == 'fishin.abucket':
                    await self.bot.get_cog('Fire API').stop()
                self.bot.unload_extension(extension)
            except Exception as exc:  # pylint: disable=broad-except
                traceback_data = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, 1))

                paginator.add_line(
                    f"{icon}\N{WARNING SIGN} `{extension}`\n```py\n{traceback_data}\n```",
                    empty=True
                )
            else:
                paginator.add_line(f"{icon} `{extension}`", empty=True)

        for page in paginator.pages:
            await ctx.send(page)

    @jsk.command(name='createvanity')
    async def jsk_createvanity(self, ctx, gid: int, code: str, inv: str):
        con = await self.bot.db.acquire()
        async with con.transaction():
            query = 'INSERT INTO vanity (\"gid\", \"code\", \"invite\") VALUES ($1, $2, $3);'
            await self.bot.db.execute(query, gid, code, inv)
        await self.bot.db.release(con)
        await self.bot.get_cog('Utility Commands').loadvanitys()
        url = 'oh-my-god'
        premium = self.bot.premiumGuilds
        if gid in premium:
            url = 'inv'
        return await ctx.success(f'Successfully created https://{url}.wtf/{code}')

    @jsk.command(name='setdesc')
    async def jsk_setdesc(self, ctx, gid: int, *, desc: str):
        await self.bot.configs[gid].set('main.description', desc)
        return await ctx.success(f'Successfully set description')

    @jsk.command(name='ack')
    async def jsk_ack(self, ctx, user: UserWithFallback, *, ack: str):  # no this doesn't mark messages as read
        if user.id not in self.bot.acknowledgements:
            con = await self.bot.db.acquire()
            async with con.transaction():
                query = 'INSERT INTO ack (\"uid\", \"acks\") VALUES ($1, $2);'
                await self.bot.db.execute(query, user.id, [ack])
            await self.bot.db.release(con)
        else:
            acks = self.bot.acknowledgements[user.id]
            acks.append(ack)
            con = await self.bot.db.acquire()
            async with con.transaction():
                query = 'UPDATE ack SET \"acks\"=$2 WHERE uid = $1;'
                await self.bot.db.execute(query, user.id, acks)
            await self.bot.db.release(con)
        await self.loadacks()
        return await ctx.success(f'Successfully added acknowledgement')

    @jsk.command(name='delack')
    async def jsk_delack(self, ctx, user: UserWithFallback, *, ack: str):  # no this doesn't mark messages as read
        if user.id in self.bot.acknowledgements and ack in self.bot.acknowledgements[user.id]:
            acks = self.bot.acknowledgements[user.id]
            if ack not in acks:
                return await ctx.error('Acknowledgement not found')
            acks.remove(ack)
            con = await self.bot.db.acquire()
            async with con.transaction():
                query = 'UPDATE ack SET \"acks\"=$2 WHERE uid = $1;'
                await self.bot.db.execute(query, user.id, acks)
            await self.bot.db.release(con)
        else:
            return await ctx.error('User has no acknowledgements')
        await self.loadacks()
        return await ctx.success(f'Successfully deleted acknowledgement')

    @jsk.command(name='alias')
    async def jsk_alias(self, ctx, user: UserWithFallback, *, alias: str):
        if alias.lower() == 'hasalias':
            return await ctx.error('"hasalias" cannot be used as an alias')
        if user.id not in self.bot.aliases['hasalias']:
            con = await self.bot.db.acquire()
            async with con.transaction():
                query = 'INSERT INTO aliases (\"uid\", \"aliases\") VALUES ($1, $2);'
                await self.bot.db.execute(query, user.id, [alias])
            await self.bot.db.release(con)
        else:
            aliases = [a for a in self.bot.aliases if a != 'hasalias' and self.bot.aliases[a] == user.id]
            aliases.append(alias)
            con = await self.bot.db.acquire()
            async with con.transaction():
                query = 'UPDATE aliases SET \"aliases\"=$2 WHERE uid = $1;'
                await self.bot.db.execute(query, user.id, aliases)
            await self.bot.db.release(con)
        await self.bot.get_cog('Settings').load_aliases()
        return await ctx.success(f'Successfully added alias')

    @jsk.command(name='delalias')
    async def jsk_delalias(self, ctx, user: UserWithFallback, *, alias: str):
        if alias in self.bot.aliases and alias != 'hasalias':
            aliases = [a for a in self.bot.aliases if a != 'hasalias' and self.bot.aliases[a] == user.id]
            if alias not in aliases:
                return await ctx.error(f'Alias doesn\'t belong to the specified user')
            aliases.remove(alias)
            con = await self.bot.db.acquire()
            async with con.transaction():
                query = 'UPDATE aliases SET \"aliases\"=$2 WHERE uid = $1;'
                await self.bot.db.execute(query, user.id, aliases)
            await self.bot.db.release(con)
        else:
            return await ctx.error('Invalid alias')
        await self.bot.get_cog('Settings').load_aliases()
        return await ctx.success(f'Successfully deleted alias')

    @jsk.group(name="voice", aliases=["vc"])
    @commands.check(vc_check)
    async def jsk_voice(self, ctx: commands.Context):
        """
        Voice-related commands.

        If invoked without subcommand, relays current voice state.
        """

        # if using a subcommand, short out
        if ctx.invoked_subcommand is not None and ctx.invoked_subcommand is not self.jsk_voice:
            return

        # give info about the current voice client if there is one
        voice = ctx.guild.voice_client

        if not voice or not voice.is_connected():
            return await ctx.send("Not connected.")

        await ctx.send(f"Connected to {voice.channel.name}, "
                       f"{'paused' if voice.is_paused() else 'playing' if voice.is_playing() else 'idle'}.")

    @jsk_voice.command(name="join", aliases=["connect"])
    async def jsk_vc_join(self, ctx: commands.Context, *,
                          destination: typing.Union[VoiceChannel, Member] = None):
        """
        Joins a voice channel, or moves to it if already connected.

        Passing a voice channel uses that voice channel.
        Passing a member will use that member's current voice channel.
        Passing nothing will use the author's voice channel.
        """

        destination = destination or ctx.author

        if isinstance(destination, discord.Member):
            if destination.voice and destination.voice.channel:
                destination = destination.voice.channel
            else:
                return await ctx.send("Member has no voice channel.")

        voice = ctx.guild.voice_client

        if voice:
            await voice.move_to(destination)
        else:
            await destination.connect(reconnect=True)

        await ctx.send(f"Connected to {destination.name}.")

    @jsk_voice.command(name="disconnect", aliases=["dc"])
    @commands.check(connected_check)
    async def jsk_vc_disconnect(self, ctx: commands.Context):
        """
        Disconnects from the voice channel in this guild, if there is one.
        """

        voice = ctx.guild.voice_client

        await voice.disconnect()
        await ctx.send(f"Disconnected from {voice.channel.name}.")

    @jsk_voice.command(name="stop")
    @commands.check(playing_check)
    async def jsk_vc_stop(self, ctx: commands.Context):
        """
        Stops running an audio source, if there is one.
        """

        voice = ctx.guild.voice_client

        voice.stop()
        await ctx.send(f"Stopped playing audio in {voice.channel.name}.")

    @jsk_voice.command(name="pause")
    @commands.check(playing_check)
    async def jsk_vc_pause(self, ctx: commands.Context):
        """
        Pauses a running audio source, if there is one.
        """

        voice = ctx.guild.voice_client

        if voice.is_paused():
            return await ctx.send("Audio is already paused.")

        voice.pause()
        await ctx.send(f"Paused audio in {voice.channel.name}.")

    @jsk_voice.command(name="resume")
    @commands.check(playing_check)
    async def jsk_vc_resume(self, ctx: commands.Context):
        """
        Resumes a running audio source, if there is one.
        """

        voice = ctx.guild.voice_client

        if not voice.is_paused():
            return await ctx.send("Audio is not paused.")

        voice.resume()
        await ctx.send(f"Resumed audio in {voice.channel.name}.")

    @jsk_voice.command(name="volume")
    @commands.check(playing_check)
    async def jsk_vc_volume(self, ctx: commands.Context, *, percentage: float):
        """
        Adjusts the volume of an audio source if it is supported.
        """

        volume = max(0.0, min(1.0, percentage / 100))

        source = ctx.guild.voice_client.source

        if not isinstance(source, discord.PCMVolumeTransformer):
            return await ctx.send("This source doesn't support adjusting volume or "
                                  "the interface to do so is not exposed.")

        source.volume = volume

        await ctx.send(f"Volume set to {volume * 100:.2f}%")

    @jsk_voice.command(name="play", aliases=["play_local"])
    @commands.check(connected_check)
    async def jsk_vc_play(self, ctx: commands.Context, *, uri: str):
        """
        Plays audio direct from a URI.

        Can be either a local file or an audio resource on the internet.
        """

        voice = ctx.guild.voice_client

        if voice.is_playing():
            voice.stop()

        # remove embed maskers if present
        uri = uri.lstrip("<").rstrip(">")

        voice.play(discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(uri)))
        await ctx.send(f"Playing in {voice.channel.name}.")

    @jsk_voice.command(name="youtube_dl", aliases=["youtubedl", "ytdl", "yt"])
    @commands.check(connected_check)
    async def jsk_vc_youtube_dl(self, ctx: commands.Context, *, url: str):
        """
        Plays audio from youtube_dl-compatible sources.
        """

        if not youtube_dl:
            return await ctx.send("youtube_dl is not installed.")

        voice = ctx.guild.voice_client

        if voice.is_playing():
            voice.stop()

        # remove embed maskers if present
        url = url.lstrip("<").rstrip(">")

        voice.play(discord.PCMVolumeTransformer(BasicYouTubeDLSource(url)))
        await ctx.send(f"Playing in {voice.channel.name}.")

    @jsk.command(name="su")
    async def jsk_su(self, ctx: commands.Context, target: UserWithFallback, *, command_string: str):
        """
        Run a command as someone else.

        This will try to resolve to a Member, but will use a User if it can't find one.
        """

        if ctx.guild:
            # Try to upgrade to a Member instance
            # This used to be done by a Union converter, but doing it like this makes
            #  the command more compatible with chaining, e.g. `jsk in .. jsk su ..`
            target = ctx.guild.get_member(target.id) or target

        alt_ctx = await copy_context_with(ctx, author=target, content=ctx.prefix + command_string)

        if alt_ctx.command is None:
            if alt_ctx.invoked_with is None:
                return await ctx.send('This bot has been hard-configured to ignore this user.')
            return await ctx.send(f'Command "{alt_ctx.invoked_with}" is not found')

        return await alt_ctx.command.invoke(alt_ctx)

    @jsk.command(name="in")
    async def jsk_in(self, ctx: commands.Context, channel: TextChannel, *, command_string: str):
        """
        Run a command as if it were in a different channel.
        """

        alt_ctx = await copy_context_with(ctx, channel=channel, content=ctx.prefix + command_string)

        if alt_ctx.command is None:
            return await ctx.send(f'Command "{alt_ctx.invoked_with}" is not found')

        return await alt_ctx.command.invoke(alt_ctx)

    @jsk.command(name="sudo")
    async def jsk_sudo(self, ctx: commands.Context, *, command_string: str):
        """
        Run a command bypassing all checks and cooldowns.

        This also bypasses permission checks so this has a high possibility of making a command raise.
        """

        alt_ctx = await copy_context_with(ctx, content=ctx.prefix + command_string)

        if alt_ctx.command is None:
            return await ctx.send(f'Command "{alt_ctx.invoked_with}" is not found')

        return await alt_ctx.command.reinvoke(alt_ctx)

    @jsk.command(name="repeat")
    async def jsk_repeat(self, ctx: commands.Context, times: int, *, command_string: str):
        """
        Runs a command multiple times in a row.
        This acts like the command was invoked several times manually, so it obeys cooldowns.
        """

        with self.submit(ctx):  # allow repeats to be cancelled
            for _ in range(times):
                alt_ctx = await copy_context_with(ctx, content=ctx.prefix + command_string)

                if alt_ctx.command is None:
                    return await ctx.send(f'Command "{alt_ctx.invoked_with}" is not found')

                await alt_ctx.command.reinvoke(alt_ctx)

    @jsk.command(name="debug", aliases=["dbg"])
    async def jsk_debug(self, ctx: commands.Context, *, command_string: str):
        """
        Run a command timing execution and catching exceptions.
        """

        alt_ctx = await copy_context_with(ctx, content=ctx.prefix + command_string)

        if alt_ctx.command is None:
            return await ctx.send(f'Command "{alt_ctx.invoked_with}" is not found')

        start = time.perf_counter()

        async with ReplResponseReactor(ctx.message):
            with self.submit(ctx):
                await alt_ctx.command.invoke(alt_ctx)

        end = time.perf_counter()
        return await ctx.send(f"Command `{alt_ctx.command.qualified_name}` finished in {end - start:.3f}s.")

    @jsk.command(name="source", aliases=["src"])
    async def jsk_source(self, ctx: commands.Context, *, command_name: str):
        """
        Displays the source code for a command.
        """

        command = self.bot.get_command(command_name)
        if not command:
            return await ctx.send(f"Couldn't find command `{command_name}`.")

        try:
            source_lines, _ = inspect.getsourcelines(command.callback)
        except (TypeError, OSError):
            return await ctx.send(f"Was unable to retrieve the source for `{command}` for some reason.")

        # getsourcelines for some reason returns WITH line endings
        source_lines = ''.join(source_lines).split('\n')

        paginator = WrappedPaginator(prefix='```py', suffix='```', max_size=1985)
        for line in source_lines:
            paginator.add_line(line)

        interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
        await interface.send_to(ctx)

    @jsk.command(name="shutdown", aliases=["logout"])
    async def jsk_shutdown(self, ctx: commands.Context):
        """
        Logs this bot out.
        """

        await ctx.send("Logging out now..")
        await ctx.bot.logout()


def setup(bot: commands.Bot):
    """
    Adds the Jishaku cog to the bot.
    """

    bot.add_cog(Jishaku(bot=bot))
    bot.logger.info(f'$GREENLoaded Jishaku')
