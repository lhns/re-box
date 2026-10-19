// Start REA's Ghidra bridge inside this (ghidra-mcp) JVM against the current program.
// Run by the re-box attach helper (helper.mjs) through /run_ghidra_script; returns once the bridge
// runs on its own thread, because that endpoint holds a request thread and a timeout monitor.
// Args: <descriptor> <bridge script dir> <helper pid> <script log> <run id> <target sha256>
//
// Keep this file free of compile-time references to ghidra.app.plugin.core.osgi: bnd would
// import that package next to the generated activator's import of it, Felix refuses the
// duplicate, and every script in ~/ghidra_scripts stops loading.
// @category re-box
import java.io.File;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.locks.Lock;

import generic.jar.ResourceFile;
import ghidra.app.script.GhidraScript;
import ghidra.app.script.GhidraScriptUtil;
import ghidra.app.script.GhidraState;
import ghidra.app.script.ScriptControls;
import ghidra.framework.ShutdownHookRegistry;
import ghidra.framework.model.Project;
import ghidra.program.model.listing.Program;
import ghidra.program.util.ProgramLocation;
import ghidra.util.task.ConsoleTaskMonitor;

public class ReboxReaAttach extends GhidraScript {
    // helper.mjs matches this to import the target into a folder of its own.
    private static final String SHA_MISMATCH = "REBOX_SHA_MISMATCH";
    private static final String NO_RETURN_FIX_DONE = "REA no-return fix applied";

    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        if (a.length != 6) {
            throw new IllegalArgumentException(
                "usage: <descriptor> <bridge dir> <helper pid> <log> <run id> <sha256>");
        }
        Program program = currentProgram;
        String sha = program.getExecutableSHA256();
        if (sha == null || !sha.toLowerCase(Locale.ROOT).equals(a[5].toLowerCase(Locale.ROOT))) {
            throw new IllegalStateException(SHA_MISMATCH + ": " + program.getDomainFile().getPathname()
                + " holds other bytes than REA's target");
        }
        Project project = state.getProject();
        long helperPid = Long.parseLong(a[2]);
        ResourceFile dir = new ResourceFile(new File(a[1]));
        Object host = GhidraScriptUtil.class.getMethod("getBundleHost").invoke(null);
        host.getClass().getMethod("enable", ResourceFile.class).invoke(host, dir);
        PrintWriter log = new PrintWriter(new FileWriter(a[3], true), true);
        Lock writeLock = ghidraMcpWriteLock(log);

        // REA runs this post-script after each of its fresh imports; the shared program needs it once.
        if (!program.getOptions(Program.PROGRAM_INFO).getBoolean(NO_RETURN_FIX_DONE, false)) {
            GhidraScript fix = instance(dir, "ReaGhidraNoReturnFix.java", log);
            fix.execute(stateFor(project, program), new ScriptControls(log, log, monitor));
            int tx = program.startTransaction("re-box: REA no-return fix applied");
            try {
                program.getOptions(Program.PROGRAM_INFO).setBoolean(NO_RETURN_FIX_DONE, true);
            }
            finally {
                program.endTransaction(tx, true);
            }
        }

        // Compile and load here, so a build error fails this request rather than the thread.
        GhidraScript bridge = instance(dir, "ReaGhidraBridge.java", log);
        bridge.setScriptArgs(new String[] { a[0] });
        if (writeLock != null) {
            Field f = bridge.getClass().getDeclaredField("reboxWriteLock");  // added by patch-rea.py
            f.setAccessible(true);
            f.set(bridge, writeLock);
        }
        Object owner = new Object();
        program.addConsumer(owner);  // the program stays open while the bridge serves
        Thread serve = new Thread(() -> {
            try {
                bridge.execute(stateFor(project, program),
                    new ScriptControls(log, log, new ConsoleTaskMonitor()));
            }
            catch (Throwable e) {
                e.printStackTrace(log);
            }
            finally {
                program.release(owner);
                log.println("re-box: REA bridge " + a[4] + " ended");
                log.close();
            }
        }, "rea-bridge-" + a[4]);
        serve.setDaemon(true);
        serve.start();

        // The bridge ends when REA's connection closes. If REA dies before connecting, it would
        // block in accept() forever: tie it to the helper process, which REA always kills.
        // Interrupting a thread blocked on an NIO channel closes the channel.
        Thread watch = new Thread(() -> {
            try {
                while (serve.isAlive()) {
                    if (!ProcessHandle.of(helperPid).map(ProcessHandle::isAlive).orElse(false)) {
                        serve.interrupt();
                        return;
                    }
                    Thread.sleep(1000);
                }
            }
            catch (InterruptedException e) {
                // JVM shutting down
            }
        }, "rea-bridge-watch-" + a[4]);
        watch.setDaemon(true);
        watch.start();
        println("REA bridge " + a[4] + " serving " + program.getDomainFile().getPathname()
            + (writeLock == null ? " (without ghidra-mcp's write lock)" : ""));
    }

    /**
     * ghidra-mcp's DirectThreadingStrategy lock, which its executeWrite endpoints hold for their
     * whole transaction. Ghidra merges concurrent transactions into one, so an aborted REA edit
     * would also discard a gh_ edit made meanwhile; the bridge holds this lock around its edits.
     * The server object is reachable only through the shutdown hook it registers
     * (ShutdownHookRegistry.hooks -> hook.r = server::stop -> server.threadingStrategy.globalLock).
     * Null, logged, when ghidra-mcp's internals changed: the bridge then runs without it.
     */
    private static Lock ghidraMcpWriteLock(PrintWriter log) {
        try {
            Field hooksField = ShutdownHookRegistry.class.getDeclaredField("hooks");
            hooksField.setAccessible(true);
            List<Object> hooks;
            synchronized (ShutdownHookRegistry.class) {
                hooks = new ArrayList<>((Collection<?>) hooksField.get(null));
            }
            for (Object hook : hooks) {
                Field r = hook.getClass().getDeclaredField("r");
                r.setAccessible(true);
                Object runnable = r.get(hook);
                for (Field captured : runnable.getClass().getDeclaredFields()) {
                    captured.setAccessible(true);
                    Object server = captured.get(runnable);
                    if (server == null || !server.getClass().getName()
                            .equals("com.xebyte.headless.GhidraMCPHeadlessServer")) {
                        continue;
                    }
                    Object strategy = field(server, "threadingStrategy");
                    return (Lock) field(strategy, "globalLock");
                }
            }
            log.println("re-box: ghidra-mcp's server object not found; REA edits run without its write lock");
        }
        catch (ReflectiveOperationException | RuntimeException e) {
            log.println("re-box: ghidra-mcp's write lock not reachable (" + e + "); REA edits run without it");
        }
        return null;
    }

    private static Object field(Object owner, String name) throws ReflectiveOperationException {
        Field f = owner.getClass().getDeclaredField(name);
        f.setAccessible(true);
        return f.get(owner);
    }

    private static GhidraScript instance(ResourceFile dir, String name, PrintWriter log)
            throws Exception {
        ResourceFile file = new ResourceFile(dir, name);
        GhidraScript script = GhidraScriptUtil.getProvider(file).getScriptInstance(file, log);
        if (script == null) {
            throw new IllegalStateException("could not load " + file);
        }
        return script;
    }

    private static GhidraState stateFor(Project project, Program program) {
        ProgramLocation where = program.getMinAddress() == null ? null
            : new ProgramLocation(program, program.getMinAddress());
        return new GhidraState(null, project, program, where, null, null);
    }
}
