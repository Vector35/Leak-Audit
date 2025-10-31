# Binary Ninja Leak Audit Plugin
This is a 99% vibe coded plugin created by Peter and ChatGPT 5.

_A Binary Ninja plugin to audit and visualize leaked BinaryView references in memory._

## Description:

A debugging tool for Binary Ninja plugin developers to detect and diagnose memory leaks related to BinaryView objects. The plugin helps identify Python references that prevent BinaryView instances from being garbage collected, which can lead to memory leaks.

### Features:

- **List Live BinaryViews**: Display all BinaryView objects currently in memory with reference counts
- **Inspect References**: Drill down into the reference chain for any BinaryView to identify what's holding it in memory
- **Visual Graphs**: Generate reference graphs using objgraph to visualize the complete backref chain
- **Noise Filtering**: Automatically filters out console, interpreter, and traceback references to focus on real leaks

The plugin adds a "Leak Audit" menu under Tools with three commands:
- **List Live BinaryViews**: Shows all BinaryView instances with refcount and interesting referrer counts
- **Inspect BV by Index...**: Displays a filtered reference tree for a specific BinaryView
- **Backrefs Graphs for All (objgraph)**: Generates visual reference graphs for all BinaryViews and displays them in an HTML report

### Use Cases:

- Debugging plugin memory leaks
- Understanding why BinaryViews aren't being released after closing files
- Identifying circular references or cached objects holding BinaryView references
- Educational tool for understanding Python garbage collection in Binary Ninja


## Required Dependencies

The following dependencies are required for this plugin:

**Core functionality**: No external dependencies

**Graph visualization (optional)**: 
- `objgraph` - Install via: `pip install objgraph`
- `graphviz` - Install via system package manager (e.g., `brew install graphviz`, `apt install graphviz`, or download from [graphviz.org](https://graphviz.org/download/))

The graph visualization feature will be disabled if these dependencies are not available, but all other functionality will work normally.

## Usage

1. Open Binary Ninja and load any binary. Do things that cause the leak to occur.
2. Go to **Command Palette → Leak Audit**
3. Choose one of the available commands:
   - Start with **List Live BinaryViews** to see what's in memory
   - Use **Inspect BV by Index...** to investigate a specific BinaryView's references
   - Use **Backrefs Graphs for All** to generate visual diagrams (requires objgraph)

### Configuration

Edit the constants at the top of `leak_audit.py` to customize behavior:

```python
DEFAULT_MAX_DEPTH = 3           # How deep to traverse reference chains
DEFAULT_PER_NODE_LIMIT = 20     # Max referrers to show per object
SHOW_REFCOUNTS = True           # Display sys.getrefcount() values
```

## Tips

- The plugin filters out console and interpreter references to reduce noise
- Run garbage collection is automatic, but you can force it via the Python console: `import gc; gc.collect()`

## License

This plugin is released under an MIT license.

## Metadata Version

2

