ui = None
logfile = None


def log(text):
    if logfile:
        logfile.write(text + "\n")
        logfile.flush()
    if ui:
        ui.log(text)
    else:
        # Shouldn't normally happen, but this can happen when debugging
        # with write == True
        print(text)
