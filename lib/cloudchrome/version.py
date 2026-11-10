"""Cloud-Chrome version.

The version identifies the application in `cloudchrome version`, in the backup
manifest and in the runtime logs.
"""

__version__ = "2.0.0"

# The backup manifest format is versioned independently of the application, so
# an archive written by one release stays readable by the next one.
BACKUP_FORMAT_VERSION = 1
