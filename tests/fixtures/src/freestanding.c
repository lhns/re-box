/* re-box test fixture: libc-free code for the targets REA runs with special Ghidra profiles
 * (raw blob, seeds, MIPS soft-float). Built by the Dockerfile `fixtures` stage. */
#define KEEP __attribute__((noinline, used))

KEEP int compute(int n) {
    return n * 1337 + 0x2a2a;
}

KEEP int classify(int code) {
    switch (code) {
    case 0: return 100;
    case 1: return 211;
    case 2: return 307;
    case 3: return 401;
    case 4: return 503;
    case 7: return 997;
    default: return -1;
    }
}

/* Float in the signature, but no arithmetic that needs libgcc's soft-float helpers. */
KEEP float negate(float x) {
    return -x;
}

/* Never called and never referenced: auto-analysis does not find it; the seeds file points at it. */
KEEP int hidden_seed_target(int a, int b) {
    return (a ^ 0x5eed) * b + 0x1234;
}

/* The first function in .text, so a raw blob built from it has its entry at offset 0. */
__attribute__((section(".text.startup"), used)) void _start(void) {
    int r = classify(compute(3));
    volatile float f = negate(2.5f);
    r += *(volatile int *)&f & 0xff;
#if defined(__mips__)
    __asm__ volatile("li $v0, 4001\n\tmove $a0, %0\n\tsyscall" : : "r"(r) : "v0", "a0");
#else
    __asm__ volatile("mov $60, %%eax\n\tsyscall" : : "D"(r) : "rax");
#endif
    for (;;) {}
}
