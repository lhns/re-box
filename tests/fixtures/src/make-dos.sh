#!/bin/sh
# Hand-assembled DOS fixtures: a .COM and a minimal MZ .EXE that print "rebox" through a
# subroutine and exit with int 21h/4Ch. Usage: make-dos.sh <outdir>
set -eu
out=$1
bytes() { for b; do printf "\\$(printf %03o "0x$b")"; done; }

# COM, loaded at CS:0100. 0100 call 010B; 0103 mov ax,4C00h; int 21h; 3 nops;
# 010B sub: mov dx,0113h; mov ah,9; int 21h; ret; 0113 "rebox$".
{
  bytes E8 08 00  B8 00 4C  CD 21  90 90 90
  bytes BA 13 01  B4 09  CD 21  C3
  printf 'rebox$'
} > "$out/hello.com"

# MZ EXE: 28-byte header padded to 2 paragraphs, no relocations, CS:IP = 0:0, SS:SP = 0:0100.
# code: mov ax,cs; mov ds,ax; call sub; mov ax,4C00h; int 21h;
# sub (offset 12): mov dx,0014h; mov ah,9; int 21h; ret; 0014 "rebox$". File size 32+26 = 58.
{
  bytes 4D 5A  3A 00  01 00  00 00  02 00  20 00  FF FF  00 00  00 01  00 00  00 00  00 00  1C 00  00 00
  bytes 00 00 00 00
  bytes 8C C8  8E D8  E8 05 00  B8 00 4C  CD 21
  bytes BA 14 00  B4 09  CD 21  C3
  printf 'rebox$'
} > "$out/hello_mz.exe"
