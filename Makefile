BASE ?= ../mini-device-player
CLI ?= $(BASE)/.tools/arduino-cli
FQBN := m5stack:esp32:m5stack_stopwatch:PSRAM=opi,USBMode=hwcdc,CDCOnBoot=cdc,PartitionScheme=app3M_fat9M_16MB

.PHONY: build test preview
build:
	$(CLI) compile --fqbn '$(FQBN)' --build-path $(CURDIR)/build firmware/usb_player
test:
	/usr/bin/python3 -m unittest discover -s tests -v
preview:
	/usr/bin/python3 -m badge_stream --preview
