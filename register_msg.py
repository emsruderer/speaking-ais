"""Deliver AIS warning messages without storing text or audio on disk."""

def add_msg(msg, speech_api=None):
    """Speak an AIS message; neither text nor audio is persisted."""
    if speech_api is None:
        from speech import speak
    else:
        speak = speech_api.speak
    speak(msg)


if __name__ == '__main__':
    add_msg("AIS voice announcement system started.")