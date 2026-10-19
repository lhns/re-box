/* re-box test fixture. The e2e suite asserts on these values; keep them in sync with
 * tests/e2e/conftest.py (COMPUTE_CONSTANT, MAGIC_STRING, SHAPE_STRING). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct shape {
    int kind;
    int width;
    int height;
    const char *label;
};

static const char *MAGIC = "rebox-fixture-magic-7f3a";
static const char *SHAPE_NAME = "rebox-shape-label";

/* compute(n) = n * 1337 + 0x2a2a: the decompiler must show 0x539 (1337) and 0x2a2a. */
__attribute__((noinline)) int compute(int n) {
    return n * 1337 + 0x2a2a;
}

__attribute__((noinline)) int classify(int code) {
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

__attribute__((noinline)) int area(const struct shape *s) {
    if (s->kind == 1)
        return s->width * s->height;
    return (s->width * s->height) / 2;
}

int main(int argc, char **argv) {
    struct shape s = { argc, 6, 7, SHAPE_NAME };
    int n = argc > 1 ? atoi(argv[1]) : 3;
    printf("%s %s %d %d %d\n", MAGIC, s.label, compute(n), classify(n), area(&s));
    return (int)strlen(MAGIC) == 0;
}
