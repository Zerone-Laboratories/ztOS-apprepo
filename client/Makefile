# The phone's side of the ztOS store: zrn (install, update, remove, store) and
# zrn-exec (runs a .zexec). It goes where SDK v1's `make install` put its
# tools, so /usr/bin/zrn, /usr/bin/zrn-exec and every app-list entry and login
# unit that names them keep working.
#
#   make check
#   sudo make install PREFIX=/usr
PREFIX ?= /usr/local
TOOLS = $(DESTDIR)$(PREFIX)/lib/zrn-sdk/tools

check:
	python3 -I test_client.py

install:
	install -d $(TOOLS)/zrnlib $(DESTDIR)$(PREFIX)/bin
	install -m755 zrn zrn-exec $(TOOLS)/
	install -m644 zrnlib/*.py $(TOOLS)/zrnlib/
	rm -rf $(TOOLS)/zrnlib/__pycache__
	ln -sf ../lib/zrn-sdk/tools/zrn $(DESTDIR)$(PREFIX)/bin/zrn
	ln -sf ../lib/zrn-sdk/tools/zrn-exec $(DESTDIR)$(PREFIX)/bin/zrn-exec

.PHONY: check install
