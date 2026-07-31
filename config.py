PURPLE       = 0x9B59B6
PURPLE_DARK  = 0x6C3483
PURPLE_LIGHT = 0xBB8FCE
GREEN        = 0x2ECC71
RED          = 0xE74C3C
ORANGE       = 0xF39C12
BLUE         = 0x3498DB
GREY         = 0x95A5A6

COOLDOWN_SECONDS = 300

CATEGORY_QUESTIONS = {
    'General Support': [
        ('Subject',       'What is the subject of your issue?',        False),
        ('Description',   'Please describe your issue in detail.',      False),
    ],
    'Staff Apply': [
        ('Question:1', 'Why do you want to become a staff member?', False),
        ('Question:2',             'Do you uave any previous staff experience if yes, explain.', False),
    ],
    'Report User': [
        ('Reported User',   'Username, display name, or ID of the user you are reporting.', False),
        ('Incident Detail', 'Please describe the incident in full detail.',                  False),
    ],
    'Partnership': [
        ('Server Name & Size', 'What is your server name and how many members do you have?', False),
        ('Proposal',           'Describe your partnership proposal.',                         False),
    ],
    'Support Ticket': [
        ('Question:1', ' What do you need support with?', False),
        ('Question:2',           'Describe your issue .', False),
    ],
}
