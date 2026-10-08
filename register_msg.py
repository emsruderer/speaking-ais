"""Deliver AIS warning messages without storing text or audio on disk."""

def add_msg(msg):
    """Speak an AIS message; neither text nor audio is persisted."""
    from speech import speak

    speak(msg)


if __name__ == '__main__':
    add_msg("AIS voice announcement system started.")