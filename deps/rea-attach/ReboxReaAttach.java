// Start REA's Ghidra bridge inside this (ghidra-mcp) JVM against the current program.
// Run by the re-box attach helper (helper.mjs) through /run_ghidra_script, after the helper
// imported the target with REA's loader options and scripts; returns once the bridge runs on its
// own thread, because that endpoint holds a request thread and a timeout monitor.
// Args: <descriptor> <bridge script dir> <helper pid> <script log> <run id> <target sha256>
//
// Keep this file free of compile-time references to ghidra.app.plugin.core.osgi and to
// ghidra-mcp's classes: bnd would import those packages next to the generated activator's import
// of the first, Felix refuses the duplicate, and every script in ~/ghidra_scripts stops loading.
// ghidra-mcp is reached through the system class loader instead (deps/ghidra-mcp/patches/0002).
// @category re-box
import java.io.File;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.lang.reflect.Field;
import java.util.Locale;
import java.util.concurrent.locks.Lock;

import generic.jar.ResourceFile;
import ghidra.app.script.GhidraScript;
import ghidra.app.script.GhidraScriptUtil;
import ghidra.app.script.GhidraState;
import ghidra.app.script.ScriptControls;
import ghidra.framework.model.Project;
import ghidra.program.model.listing.Program;
import ghidra.program.util.ProgramLocation;
import ghidra.util.task.ConsoleTaskMonitor;

public class ReboxReaAttach extends GhidraScript {

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
            throw new IllegalStateException(program.getDomainFile().getPathname()
                + " holds other bytes than REA's target");
        }
        Project project = state.getProject();
        long helperPid = Long.parseLong(a[2]);
        PrintWriter log = new PrintWriter(new FileWriter(a[3], true), true);
        Lock writeLock = (Lock) ghidraMcp("com.xebyte.headless.DirectThreadingStrategy", "writeLock", log);

        // Compile and load here, so a build error fails this request rather than the thread.
        GhidraScript bridge = loadBridge(new File(a[1], "ReaGhidraBridge.java"), log);
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
                ProgramLocation where = program.getMinAddress() == null ? null
                    : new ProgramLocation(program, program.getMinAddress());
                bridge.execute(new GhidraState(null, project, program, where, null, null),
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
     * The bridge script, its directory enabled as a bundle of its own (it ships its own
     * activator). Loaded under ghidra-mcp's script-load lock: two bundle builds at once in one
     * JVM fail on Ghidra's build lock (OverlappingFileLockException).
     */
    private static GhidraScript loadBridge(File file, PrintWriter log) throws Exception {
        ResourceFile source = new ResourceFile(file);
        Object lock = ghidraMcpField("com.xebyte.core.ImportScripts", "LOAD_LOCK", log);
        synchronized (lock != null ? lock : ReboxReaAttach.class) {
            Object host = GhidraScriptUtil.class.getMethod("getBundleHost").invoke(null);
            host.getClass().getMethod("enable", ResourceFile.class).invoke(host, source.getParentFile());
            GhidraScript script = GhidraScriptUtil.getProvider(source).getScriptInstance(source, log);
            if (script == null) {
                throw new IllegalStateException("could not load " + file);
            }
            return script;
        }
    }

    private static Object ghidraMcp(String cls, String method, PrintWriter log) {
        try {
            return ClassLoader.getSystemClassLoader().loadClass(cls).getMethod(method).invoke(null);
        }
        catch (ReflectiveOperationException | RuntimeException e) {
            log.println("re-box: " + cls + "." + method + "() not found (" + e + "); REA edits run without it");
            return null;
        }
    }

    private static Object ghidraMcpField(String cls, String field, PrintWriter log) {
        try {
            return ClassLoader.getSystemClassLoader().loadClass(cls).getField(field).get(null);
        }
        catch (ReflectiveOperationException | RuntimeException e) {
            log.println("re-box: " + cls + "." + field + " not found (" + e + ")");
            return null;
        }
    }
}
