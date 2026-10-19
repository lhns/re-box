// Start REA's Ghidra bridge inside this (ghidra-mcp) JVM against the current program.
// Run by the re-box attach helper through /run_ghidra_script; returns once the bridge is serving
// on its own thread, because that endpoint holds a request thread and a timeout monitor.
// Args: <descriptor> <bridge script dir> <helper pid> <script log> <run id>
// @category re-box
import java.io.File;
import java.io.FileWriter;
import java.io.PrintWriter;

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
    private static final String NO_RETURN_FIX_DONE = "REA no-return fix applied";

    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        if (a.length != 5) throw new IllegalArgumentException("usage: <descriptor> <bridge dir> <helper pid> <log> <run id>");
        Program program = currentProgram;
        Project project = state.getProject();
        long helperPid = Long.parseLong(a[2]);
        ResourceFile dir = new ResourceFile(new File(a[1]));
        // Reflection: a compile-time reference to ghidra.app.plugin.core.osgi makes bnd import that
        // package next to the generated activator's import of it, Felix refuses the duplicate,
        // and every script in ~/ghidra_scripts stops loading.
        Object host = GhidraScriptUtil.class.getMethod("getBundleHost").invoke(null);
        host.getClass().getMethod("enable", ResourceFile.class).invoke(host, dir);
        PrintWriter log = new PrintWriter(new FileWriter(a[3], true), true);

        // REA runs this post-script after every fresh import; the shared program needs it once.
        if (!program.getOptions(Program.PROGRAM_INFO).getBoolean(NO_RETURN_FIX_DONE, false)) {
            GhidraScript fix = instance(dir, "ReaGhidraNoReturnFix.java", log);
            fix.execute(stateFor(project, program), new ScriptControls(log, log, monitor));
            int tx = program.startTransaction("re-box: mark no-return fix");
            try {
                program.getOptions(Program.PROGRAM_INFO).setBoolean(NO_RETURN_FIX_DONE, true);
            } finally {
                program.endTransaction(tx, true);
            }
        }

        // Compile and load here, so a build error fails this request rather than the thread.
        GhidraScript bridge = instance(dir, "ReaGhidraBridge.java", log);
        bridge.setScriptArgs(new String[] { a[0] });
        Object owner = new Object();
        program.addConsumer(owner);  // the program stays open while the bridge serves
        Thread serve = new Thread(() -> {
            try {
                bridge.execute(stateFor(project, program), new ScriptControls(log, log, new ConsoleTaskMonitor()));
            } catch (Throwable e) {
                e.printStackTrace(log);
            } finally {
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
            } catch (InterruptedException e) {
                // JVM shutting down
            }
        }, "rea-bridge-watch-" + a[4]);
        watch.setDaemon(true);
        watch.start();
        println("REA bridge " + a[4] + " serving " + program.getDomainFile().getPathname());
    }

    private static GhidraScript instance(ResourceFile dir, String name, PrintWriter log) throws Exception {
        ResourceFile file = new ResourceFile(dir, name);
        GhidraScript script = GhidraScriptUtil.getProvider(file).getScriptInstance(file, log);
        if (script == null) throw new IllegalStateException("could not load " + file);
        return script;
    }

    private static GhidraState stateFor(Project project, Program program) {
        ProgramLocation where = program.getMinAddress() == null ? null : new ProgramLocation(program, program.getMinAddress());
        return new GhidraState(null, project, program, where, null, null);
    }
}
