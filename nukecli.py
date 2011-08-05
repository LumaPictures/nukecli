#!/usr/bin/env nukepy
# copyright Luma Pictures
'''
Module for using command-line strings to build, save, and execute Nuke scripts.

Basic usage syntax is:

    nukecli -<class> [knob1 value1] [knob2 value2]... [knobN valueN] [-<command1> [arg1]...]

<class> is the name of a Nuke node class (grade, read, write, merge, etc.). Node classes
can be case-insensitive, or even partial class names if the partial string will match
exactly one class. Knobs are always given in name/value pairs.

<command> one the following actions: set, push, save, execute

---------------------------
    Nodes and Knobs
---------------------------

Knobs
=====

Knobs whose value requires multiple components should have the value list passed in curly
braces. An example of a 4-component "blackpoint" value on a Grade node:

    -grade blackpoint {.015 .016 .109 .1}

Multi-dimensional knob values simply use nested brace-sets. Here's an example of setting a
Camera's Local Matrix knob:

    -camera useMatrix true matrix {{.1 .2 .3 .4} {.5 .6 .7 .8} {1 1 2 2} {3 3 4 4}}

Connections
===========

Nuke uses the concept of a stack to build node networks. The most recently created node is placed
at the top of the stack (index 0), pushing previously created nodes down.
When a node is created, its default inputs are filled by the available nodes in the stack:
the node at stack 0 is connected to input 0, stack 1 to input 1, and so on.

A Merge, for example, has 1 default input, so by default only the last created node will be
connected to it.  To use the last 2 created nodes as its inputs, simply add the 'inputs' knob
to the node command along with an integer value. For example, to "plus" the output of the last
two created nodes, you would use:

    -merge operation plus inputs 2

---------------------------
        Commands
---------------------------

There are also several unique keyword commands that can be used to perform other actions:

-set
====

 The most recently created node can be stored to a named variable using the "-set"
command followed by the desired variable name. The following code creates a Grade node with
a mono blackpoint of .015 and stores it in the variable "mygrade:"

    -grade blackpoint .015 -set mygrade


-push
=====

Named variables can be recalled at any time using the "-push" command. This will
push the corresponding node to the top of the node stack, making it available as the input
for the next node created. To recall our previous grade node for later use, we would simply call:

    -push mygrade

You can also push 0 (zero), which can be used to keep one of the inputs of a node from
connecting to anything. For example, a ScanlineRender node can take 3 inputs, but typically
only the Camera and Obj/Scene inputs are used (input indices 1 and 2). Thus, to avoid
unintentionally connecting your last-created node to the BG input (index 0) or screwing up
the input connections, you would typically want to "push 0" immediately before creating one.
The following would create a simple 3D setup, but leave the "BG" input of the ScanlineRender
node disconnected:

    -camera -set renderCam -card -scene inputs 1 -set renderScene -push renderCam
    -push renderScene -push 0 -scanlinerender inputs 3


-execute
========

Any node that can normally be executed in the Nuke GUI can be run as an executable
node using this command-line interface as well. To execute a node, simply add the "-execute"
command after the node's definition command and arguments, followed by a valid frame range:

    -write /path/to/my/file.%04d.exr -execute 1-10

If no frame range is specified, Nuke will attempt to use the node's "first" and "last" knob
values, so another way of executing the same process as above would be:

    -write /path/to/my/file.%04d.exr first 1 last 10 -execute

NOTE: All "-execute" commands are stored in the order they are entered and run in succession
after the entire script is built. Keep this in mind if you need to run something like a
CurveTool node before writing out image files.

ALSO NOTE: Only Write node execution has been tested so far.


-save
=====

 This can be used to save the Nuke script. The syntax is simply "-save" followed by a
valid path to a file. The target path's directory must already exist. If a file with the
given path already exists, a warning will be printed, and by default, the save will be skipped.
However, you can add an optional "force" argument to force the script to be overwritten:

    -save /path/to/a/script.nk force

NOTE: Save commands are added to the command stack immediately as they are found. Therefore,
inserting two save statements at different points in the command list with different target
files will result in two different .nk files, each containing the script as it existed when
the "save" command was inserted.
'''

import os
import random
import re
import subprocess
import sys

import nuke
import pynuke

from zlib import crc32

# List of valid node classes, excluding *Reader and *Writer plugins
allPlugins = pynuke.getPluginList(['^.+Reader$', '^.+Writer$'], inclusiveREs=False)

def getNukeNode(inString):
    '''
    Figures out what node class to use based on 'inString.'

    The resolution order is:
    1) Exact match
    2) Case-insensitive match
    3) Single partial match
    4) Error

    Class names that differ only by a trailing version digit are
    prioritized to return the highest version.
    '''
    rawVerRE = r'^%s\d?$' % inString

    # Exact match
    matches = []
    searchRE = re.compile(rawVerRE)
    for item in allPlugins:
        if searchRE.match(item):
            matches.append(item)
    if matches:
        matches.sort()
        return matches[-1]

    # Case-insensitive match
    matches = []
    searchRE = re.compile(rawVerRE, flags=re.IGNORECASE)
    for item in allPlugins:
        if searchRE.match(item):
            matches.append(item)
    if matches:
        matches.sort()
        return matches[-1]

    # Single partial match
    matches = []
    searchRE = re.compile(r'^%s' % inString, flags=re.IGNORECASE)
    for item in allPlugins:
        if searchRE.search(item):
            matches.append(item)
    if matches:
        if len(matches) > 1:
            raise ValueError("Input argument '%s' partially matched the following node classes:\n\t%s" % (inString, ', '.join(matches)))
        return matches[0]

    # You fucked up now
    raise ValueError("Input argument '%s' could not be matched to a node class" % inString)

def generateNodeID(varName):
    '''
    Generates a CRC32 hash from a combination of 'varName' and a
    pseudo-random number
    '''
    return 'N%s' % str(crc32(varName + str(random.random()))).replace('-', '_')

def parseLine(rawLine, regexObj):
    '''
    Uses the specified 'regexObj' to parse 'rawLine' and return a
    command-args pair as a (string, list) 2-tuple.
    '''
    if isinstance(regexObj, basestring):
        regexObj = re.compile(regexObj)
    parsedLine = regexObj.findall(rawLine)
    lineCmd = parsedLine.pop(0)
    return (lineCmd, parsedLine)

def parseCLI(rawInput):
    '''
    Converts a command-line-style string of arguments into a
    string of Nuke-style TCL commands that can be passed directly
    to nuke.tcl()
    '''
    rawInput = ' %s' % rawInput
    cmdStack = []
    execNodes = []
    varMap = {'0':'0'}
    argRE = re.compile('(\{[{}.0-9\s]+\}|[^\s]+)')
    cli = [i.strip() for i in rawInput.split(' -') if i]

    for line in cli:
        cmd, args = parseLine(line, argRE)
        if cmd == 'set' and len(args) == 1:
            varName = args[0]
            varID = generateNodeID(varName)
            varMap[varName] = varID
            cmdStack.append('set %s [stack 0]' % varID)
        elif cmd == 'push' and len(args) == 1:
            varName = args[0]
            varID = varMap[varName]
            if varName != '0':
                cmdStack.append('push $%s' % varID)
            else:
                cmdStack.append('push 0')
        elif cmd == 'execute':
            varID = generateNodeID(cmd)
            execNodes.append((varID, args[0] if args else None))
            cmdStack.append('set %s [stack 0]' % varID)
        elif cmd == 'save':
            saveFile = args.pop(0)
            if os.path.isdir(saveFile):
                print "WARNING: Script save path '%s' is a directory. Ignoring." % saveFile
                continue
            if os.path.exists(saveFile):
                print "WARNING: Script save path already exists: '%s'" % saveFile
                if not args:
                    print "WARNING: No force command found. Skipping script save."
                    continue
                if args[0] != 'force':
                    print "WARNING: Invalid 'save' syntax. Use '-save <path> force' to force-overwrite a save target. Skipping script save."
                    continue
                else:
                    print "Forcing script save."
            cmdStack.append('script_save {%s}' % saveFile)
        else:
            node = getNukeNode(cmd)
            nodeArgs = ' '.join(args) if args else ''
            cmdStack.append('%s {%s}' % (node, nodeArgs))

    # If we have nodes to execute, append those commands to the end of the stack
    for node, range in execNodes:
        if range is not None:
            cmdStack.append('execute $%s %s' % (node, range))
        else:
            cmdStack.append('execute $%s [value $%s.first]-[value $%s.last]' % (node, node, node))

    tclString = ';'.join(cmdStack) + ';'
    return tclString

if __name__ == '__main__':
    try:
        parsedCmd = parseCLI(' '.join(sys.argv[1:]))
    except ValueError:
        import traceback
        traceback.print_exc()
    else:
        print '\n%s TCL COMMAND STRING %s' % ('='*15, '='*15)
        print '%s' % parsedCmd
        print '%s\n' % ('='*50,)
        nuke.tcl("""%s""" % parsedCmd)
