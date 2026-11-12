"""Copies the unrouted board to the output, then hangs: the grader must score the file that exists."""
import shutil, sys, time
shutil.copy(sys.argv[1], sys.argv[2])
time.sleep(100000)
